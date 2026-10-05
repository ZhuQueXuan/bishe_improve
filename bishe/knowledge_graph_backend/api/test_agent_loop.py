"""Behavior tests for bounded observations, stopping, provenance and timeout adapters."""
import json
from unittest.mock import Mock, patch
from django.test import SimpleTestCase
from rest_framework.test import APITestCase
from .agent_loop import BoundedAgent, AgentFailure, AgentStop
from .graph_retrieval import RetrievalUnavailable

SOURCE_FIELDS = {'has_symptom': 'symptom', 'need_check': 'check', 'cause': 'cause',
                 'prevent': 'prevent', 'desc': 'desc'}

def fact(field='has_symptom', value='多尿', disease='糖尿病', line=1):
    return {'disease': disease, 'field': field, 'label': field, 'value': value,
            'medical_review_status': 'not_verified', 'sources': [{
                'file': 'bishe/data/medical.json', 'file_sha256': 'a'*64,
                'line': line, 'record_id': str(line), 'disease': disease,
                'field': SOURCE_FIELDS[field]}]}

def found(*facts):
    return {'status': 'found' if facts else 'no_evidence', 'facts': list(facts),
            'diseases': list(dict.fromkeys(f['disease'] for f in facts)), 'context': 'test'}

def plan(names=None, fields=None, keywords=None, medical=True):
    return json.dumps({'medical': medical, 'diseases': names if names is not None else ['糖尿病'],
                       'fields': fields or ['has_symptom'], 'keywords': keywords or []})

class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now

class AgentLoopTests(SimpleTestCase):
    def setUp(self):
        self.graph, self.text, self.model = Mock(), Mock(), Mock()
        self.graph.return_value = found(fact())
        self.text.return_value = found()
        self.model.plan_query.return_value = plan()
        self.model.knowledge_qa.return_value = '依据资料回答 [E1]'
        self.clock = Clock()
        self.agent = BoundedAgent(self.graph, self.text, lambda: self.model, clock=self.clock)

    def run_query(self, query='糖尿病有哪些症状？', history=None):
        return self.agent.retrieve(query, history or [])

    def test_enough_baseline_stops_without_extra_query(self):
        result = self.run_query()
        self.assertEqual(result['agent']['stop_reason'], 'evidence_covered')
        self.graph.assert_called_once_with('糖尿病有哪些症状？', [], None, timeout=30)
        self.text.assert_not_called()
        self.assertEqual(result['agent']['counts'], {'retrieval':1,'understand':1,'answer':0})
        self.assertTrue(result['context'])

    def test_observation_drives_missing_field_retrieval_and_stop(self):
        self.model.plan_query.return_value = plan(fields=['cause','prevent'])
        self.graph.side_effect = [found(fact('cause', '原文病因')), found(fact('prevent', '原文预防'))]
        result = self.run_query('糖尿病病因和预防')
        self.assertEqual(self.graph.call_args_list[1].args, ('糖尿病', [], ['prevent']))
        self.assertEqual(result['agent']['counts']['retrieval'], 2)
        self.assertEqual({f['field'] for f in result['facts']}, {'cause','prevent'})
        self.assertEqual(result['agent']['steps'][1]['coverage_after']['missing'],
                         [{'disease':'糖尿病','field':'prevent'}])
        self.assertEqual(result['agent']['stop_reason'], 'evidence_covered')

    def test_changed_observation_can_narrow_next_graph_action(self):
        self.model.plan_query.return_value = plan(fields=['cause','prevent'])
        self.graph.side_effect = [found(), found(fact('cause','病因')), found(fact('prevent','预防'))]
        result = self.run_query('糖尿病诱因和防范')
        self.assertEqual(self.graph.call_args_list[1].args[-1], ['cause','prevent'])
        self.assertEqual(self.graph.call_args_list[2].args[-1], ['prevent'])
        self.assertEqual(result['agent']['stop_reason'], 'evidence_covered')
        self.assertEqual(result['agent']['counts']['retrieval'], 3)

    def test_missing_graph_observation_can_choose_existing_fulltext(self):
        self.model.plan_query.return_value = plan(fields=['prevent'],keywords=['运动'])
        self.graph.side_effect = [found(), found()]
        self.text.return_value = found(fact('prevent','原文预防运动'))
        result=self.run_query('糖尿病怎么防范')
        self.assertEqual(result['agent']['counts']['retrieval'],3)
        self.text.assert_called_once_with('运动',limit=5,timeout=30)
        self.assertTrue(result['context'])

    def test_empty_results_never_generate(self):
        self.graph.return_value = found()
        result=self.run_query()
        self.assertFalse(result['context'])
        self.assertEqual(result['agent']['stop_reason'],'no_new_action')
        self.assertEqual(self.graph.call_count,2)
        self.model.knowledge_qa.assert_not_called()
        with self.assertRaises(ValueError): self.agent.answer('问题',result,[])

    def test_partial_evidence_does_not_claim_full_answer(self):
        self.model.plan_query.return_value=plan(fields=['cause','prevent'])
        self.graph.return_value=found(fact('cause','病因'))
        result=self.run_query('糖尿病病因和预防')
        self.assertFalse(result['context'])
        self.assertEqual(result['agent']['final_observation']['missing'],[{'disease':'糖尿病','field':'prevent'}])

    def test_all_diseases_must_cover_requested_fields(self):
        self.model.plan_query.return_value=plan(names=['糖尿病','肺炎'])
        self.graph.side_effect=[found(fact()),found(fact(disease='肺炎',line=2))]
        result=self.run_query('糖尿病和肺炎有什么症状')
        self.assertEqual(self.graph.call_args_list[1].args[0],'肺炎')
        self.assertEqual(len(result['diseases']),2)

    def test_budget_counts_baseline_and_refuses_incomplete_answer(self):
        self.agent.max_retrieval=1
        self.model.plan_query.return_value=plan(fields=['cause'])
        result=self.run_query('糖尿病病因')
        self.assertEqual(result['agent']['stop_reason'],'retrieval_budget_exhausted')
        self.assertEqual(self.graph.call_count,1)
        self.assertFalse(result['context'])

    def test_exact_duplicate_action_blocked_without_execution(self):
        self.agent._read('graph',{'query':'糖尿病','fields':['has_symptom']},[])
        with self.assertRaises(AgentStop) as error:
            self.agent._read('graph',{'query':'糖尿病','fields':['has_symptom']},[])
        self.assertEqual(error.exception.reason,'duplicate_action')
        self.assertEqual(self.graph.call_count,1)

    def test_tool_error_is_not_hidden_by_old_evidence(self):
        self.graph.side_effect=[found(fact()),RetrievalUnavailable('password=do-not-expose')]
        self.model.plan_query.return_value=plan(fields=['cause'])
        with self.assertRaises(AgentFailure) as error: self.run_query('糖尿病病因')
        self.assertEqual(error.exception.trace['stop_reason'],'tool_error')
        self.assertNotIn('do-not-expose',json.dumps(error.exception.trace))
        self.model.knowledge_qa.assert_not_called()

    def test_invalid_source_refused_before_model(self):
        invalid=fact(); invalid['sources'][0]['file']='other.json'
        self.graph.return_value=found(invalid)
        with self.assertRaises(AgentFailure): self.run_query()
        self.model.plan_query.assert_not_called()

    def test_deadline_after_slow_tool_stops_before_planner(self):
        def slow(*args,**kwargs):
            self.clock.now=91
            return found(fact())
        self.graph.side_effect=slow
        result=self.run_query()
        self.assertEqual(result['agent']['stop_reason'],'deadline_exceeded')
        self.model.plan_query.assert_not_called()
        self.assertFalse(result['context'])

    def test_remaining_budget_passed_to_planner_and_answer(self):
        def baseline(*args,**kwargs):
            self.clock.now=80
            return found(fact())
        self.graph.side_effect=baseline
        result=self.run_query()
        self.assertEqual(self.model.plan_query.call_args.kwargs['timeout'],10)
        self.clock.now=85
        self.agent.answer('糖尿病症状',result,[])
        self.assertEqual(self.model.knowledge_qa.call_args.kwargs['timeout'],5)
        self.assertEqual(self.agent.trace['counts']['answer'],1)

    def test_late_answer_is_discarded(self):
        result=self.run_query()
        def late(*args,**kwargs): self.clock.now=91; return '迟到的回答'
        self.model.knowledge_qa.side_effect=late
        self.assertIsNone(self.agent.answer('糖尿病症状',result,[]))
        self.assertEqual(self.agent.trace['stop_reason'],'deadline_exceeded')

    def test_invalid_planner_falls_back_to_validated_baseline(self):
        self.model.plan_query.return_value='not JSON'
        result=self.run_query()
        self.assertEqual(result['agent']['stop_reason'],'fallback_invalid_or_unavailable')
        self.assertTrue(result['context'])
        self.assertEqual(self.graph.call_count,1)

    def test_nonmedical_and_brand_boundaries(self):
        self.model.plan_query.return_value=json.dumps({'medical':False,'diseases':[],'fields':[],'keywords':[]})
        result=self.run_query('写代码')
        self.assertEqual(result['status'],'nonmedical')
        self.assertFalse(result['context'])
        self.graph.return_value={'status':'unsupported_field','facts':[],'diseases':[],'context':''}
        other=BoundedAgent(self.graph,self.text,lambda:self.model)
        self.model.reset_mock()
        result=other.retrieve('药品厂家',[])
        self.model.plan_query.assert_not_called()
        self.assertEqual(result['status'],'unsupported_field')

    def test_planner_and_trace_never_include_history_or_raw_evidence_text(self):
        history=[{'role':'user','content':'糖尿病敏感历史HISTORY123'}]
        result=self.run_query(history=history)
        payload=self.model.plan_query.call_args.args[1]
        self.assertEqual(json.loads(payload),{'question':'糖尿病有哪些症状？'})
        self.assertNotIn('HISTORY123',json.dumps(result['agent'],ensure_ascii=False))
        self.assertNotIn('多尿',json.dumps(result['agent'],ensure_ascii=False))

    def test_text_does_not_merge_fields_from_different_diseases(self):
        self.model.plan_query.return_value=plan(names=[],fields=['desc','cause'],keywords=['短语'])
        self.graph.return_value=found()
        self.text.return_value=found(fact('desc','短语',disease='糖尿病'),fact('cause','短语',disease='肺炎',line=2))
        result=self.run_query('查询短语相关原文')
        self.assertFalse(result['context'])
        self.assertEqual(result['agent']['stop_reason'],'no_new_action')

    def test_text_candidate_remains_source_checked_not_diagnosis(self):
        self.model.plan_query.return_value=plan(names=[],fields=['desc'],keywords=['胰岛素'])
        self.graph.return_value=found()
        self.text.return_value=found(fact('desc','胰岛素分泌不足'))
        result=self.run_query('控制血糖的激素不足资料')
        self.assertEqual(result['resolution'],'text_candidates_not_diagnosis')
        self.assertIn('不代表用户患有',result['context'])

    def test_context_limit_cannot_drop_required_fields_then_generate(self):
        self.model.plan_query.return_value=plan(fields=['cause','prevent'])
        self.graph.return_value=found(fact('cause','x'*11900),fact('prevent','y'*500))
        result=self.run_query('糖尿病病因和预防')
        self.assertFalse(result['context'])
        self.assertEqual(result['agent']['stop_reason'],'context_limit')

    def test_source_field_mismatch_refused(self):
        invalid=fact(); invalid['sources'][0]['field']='cause'
        self.graph.return_value=found(invalid)
        with self.assertRaises(AgentFailure): self.run_query()
        self.model.plan_query.assert_not_called()

    def test_exception_after_deadline_cannot_use_baseline_fallback(self):
        def late_plan(*args,**kwargs):
            self.clock.now=91
            raise TimeoutError('planner deadline')
        self.model.plan_query.side_effect=late_plan
        result=self.run_query()
        self.assertEqual(result['agent']['stop_reason'],'deadline_exceeded')
        self.assertFalse(result['context'])

class ReadTimeoutTests(SimpleTestCase):
    @patch('api.read_tools.GraphDatabase.driver')
    def test_real_driver_adapter_uses_read_mode_no_retries_and_transaction_timeout(self,driver):
        from .read_tools import bounded_read
        session=driver.return_value.__enter__.return_value.session.return_value.__enter__.return_value
        session.execute_read.return_value=found()
        bounded_read({'HOST':'bolt://local','AUTH':('user','secret')},'neo4j',lambda tx:None,(),4)
        self.assertEqual(driver.call_args.kwargs['max_transaction_retry_time'],0)
        self.assertLessEqual(driver.call_args.kwargs['connection_timeout'],4)
        self.assertEqual(driver.return_value.__enter__.return_value.session.call_args.kwargs['default_access_mode'],'READ')
        operation=session.execute_read.call_args.args[0]
        self.assertGreater(operation.timeout,0)
        self.assertLessEqual(operation.timeout,4)

    @patch('api.utils.OpenAI')
    def test_sdk_receives_answer_timeout_and_no_automatic_retry(self,client):
        from .utils import DeepSeekAPI
        api=DeepSeekAPI()
        api.generate_text('local test',timeout=3)
        client.return_value.with_options.assert_called_with(max_retries=0)
        self.assertEqual(client.return_value.with_options.return_value.chat.completions.create.call_args.kwargs['timeout'],3)

class AgentApiTests(APITestCase):
    @patch('api.views.DeepSeekAPI')
    @patch('api.views.retrieve_context')
    def test_partial_evidence_never_calls_answer(self,graph,model):
        graph.return_value=found(fact('cause','病因'))
        model.return_value.plan_query.return_value=plan(fields=['cause','prevent'])
        response=self.client.post('/api/ai/knowledge-qa/',{'query':'糖尿病病因和预防'},format='json')
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.data['has_context'])
        model.return_value.knowledge_qa.assert_not_called()
        self.assertIn('agent',response.data['retrieval'])

    @patch('api.views.DeepSeekAPI')
    @patch('api.views.retrieve_context')
    def test_answer_failure_is_service_error_without_history_write(self,graph,model):
        from .models import ChatMessage
        graph.return_value=found(fact())
        model.return_value.plan_query.return_value=plan()
        model.return_value.knowledge_qa.side_effect=RuntimeError('credential secret')
        response=self.client.post('/api/ai/knowledge-qa/',{'query':'糖尿病症状'},format='json')
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.data['retrieval']['agent']['stop_reason'],'answer_error')
        self.assertNotIn('credential',json.dumps(response.data))
        self.assertEqual(ChatMessage.objects.count(),0)
