import json
from unittest.mock import Mock, patch, ANY
from django.test import SimpleTestCase
from rest_framework.test import APITestCase
from .query_planning import validate_plan, planned_retrieval
from .graph_retrieval import RetrievalUnavailable
from .test_agent_loop import fact, found


def plan(names=None, fields=None, keywords=None, medical=True):
    return json.dumps({'medical': medical, 'diseases': names or [], 'fields': fields or [], 'keywords': keywords or []})


class PlannerTests(SimpleTestCase):
    def setUp(self):
        self.baseline = {'status': 'no_evidence', 'facts': [], 'diseases': [], 'context': ''}
        self.model, self.graph, self.text = Mock(), Mock(), Mock()
        self.graph.return_value = {'status': 'found', 'facts': [], 'context': '实际证据'}

    def run_plan(self, query='糖尿病有什么征兆？', history=None):
        return planned_retrieval(query, history or [], self.baseline, self.model, self.graph, self.text)

    def test_named_fields_read_only_parameters(self):
        self.model.plan_query.return_value = plan(['糖尿病'], ['has_symptom'])
        result = self.run_plan()
        self.graph.assert_called_once_with('糖尿病', [], ['has_symptom'])
        self.text.assert_not_called()
        self.assertEqual(result['planning']['status'], 'validated')

    def test_no_extra_history_or_sources_sent(self):
        self.model.plan_query.return_value = plan([], ['prevent'])
        self.baseline.update(diseases=['糖尿病'], context='本地原文')
        history = [{'role': 'user', 'content': '糖尿病，敏感历史XYZ'}, {'role': 'assistant', 'content': '错误病名'}]
        self.run_plan('它怎么防范？', history)
        payload = self.model.plan_query.call_args.args[1]
        self.assertEqual(json.loads(payload), {'question': '它怎么防范？'})
        self.assertNotIn('敏感历史XYZ', payload)
        self.graph.assert_called_once_with('糖尿病', history, ['prevent'])

    def test_inferred_disease_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan(plan(['糖尿病'], ['desc']), '我口渴，多尿')

    def test_bad_schema_bounds_and_code_rejected(self):
        for content in ['```json\n{}\n```', '{}', 'x'*6001,
                        plan(['糖尿病'], ['DELETE']), plan(['糖尿病'], ['desc'], ['x'*61]),
                        plan(['糖尿病'], ['desc'], ['a','b','c','d']),
                        plan(['糖尿病'], ['desc'], medical=False)]:
            with self.assertRaises(ValueError):
                validate_plan(content, '糖尿病')

    def test_planner_timeout_falls_back(self):
        self.model.plan_query.side_effect = TimeoutError()
        result = self.run_plan()
        self.assertEqual(result['context'], self.baseline['context'])
        self.assertEqual(result['planning']['status'], 'fallback_invalid_or_unavailable')
        self.graph.assert_not_called()

    def test_brand_boundary_bypasses_model(self):
        self.baseline['status'] = 'unsupported_field'
        self.run_plan('药品厂家')
        self.model.plan_query.assert_not_called()

    def test_nonmedical_no_context(self):
        self.model.plan_query.return_value = plan(medical=False)
        self.assertEqual(self.run_plan('写代码')['status'], 'nonmedical')
        self.graph.assert_not_called()

    def test_text_dedup_and_source_preserved(self):
        self.model.plan_query.return_value = plan(fields=['desc'], keywords=['胰岛素'])
        fact = {'disease':'糖尿病','field':'desc','label':'描述','value':'胰岛素分泌不足',
                'sources':[{'line':1,'start':0,'end':8}], 'medical_review_status':'not_verified'}
        self.text.return_value = {'facts':[fact,fact]}
        result = self.run_plan('控制血糖的激素不够的资料')
        self.assertEqual(len(result['facts']), 1)
        self.assertEqual(result['facts'][0]['sources'], fact['sources'])
        self.assertEqual(result['resolution'], 'text_candidates_not_diagnosis')
        self.assertIn('[E1]', result['context'])

    def test_tool_failure_not_hidden(self):
        self.model.plan_query.return_value = plan(['糖尿病'], ['cause'])
        self.graph.side_effect = RetrievalUnavailable('资料冲突')
        with self.assertRaises(RetrievalUnavailable):
            self.run_plan()


class PlannedApiTests(APITestCase):
    @patch('api.views.retrieve_phrase')
    @patch('api.views.retrieve_context')
    @patch('api.views.DeepSeekAPI')
    def test_planning_then_evidence_answer(self, model, graph, text):
        model.return_value.plan_query.return_value = plan(['糖尿病'], ['has_symptom'])
        model.return_value.knowledge_qa.return_value = '依据资料回答 [E2]'
        graph.side_effect = [found(fact('cause', '原病因资料')), found(fact())]
        result = self.client.post('/api/ai/knowledge-qa/', {'query':'糖尿病有什么征兆？'}, format='json')
        self.assertEqual(result.status_code, 200)
        model.return_value.knowledge_qa.assert_called_once_with('糖尿病有什么征兆？', ANY, conversation_history=[], timeout=ANY)
        self.assertEqual(result.data['retrieval']['agent']['counts']['retrieval'], 2)

    @patch('api.views.retrieve_context')
    @patch('api.views.DeepSeekAPI')
    def test_no_evidence_after_valid_plan_no_answer_call(self, model, graph):
        model.return_value.plan_query.return_value = plan(['糖尿病'], ['cause'])
        graph.return_value = {'status':'no_evidence','context':'','facts':[]}
        result = self.client.post('/api/ai/knowledge-qa/', {'query':'糖尿病诱因'}, format='json')
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.data['has_context'])
        model.return_value.knowledge_qa.assert_not_called()
