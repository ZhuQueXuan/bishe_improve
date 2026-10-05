"""Formal API integration regressions; models/sockets are mocked, DB is in-memory."""
import copy
import json
import uuid
from unittest.mock import Mock, patch
from django.contrib.auth.models import User
from rest_framework.test import APITestCase
from .chat_runtime import (ChatAgent, Checkpoints, RUNTIME_ROOT, source_catalog,
                           prepare_context, ProductionPlanner, ROOT)
from .planning_tools import SnapshotTools
from .planning_runtime import BoundaryError, entity_id
from .planning_policies import MockDynamicPolicy
from .models import Conversation, ChatMessage
from .test_agent_loop import fact, found


class Planner(MockDynamicPolicy):
    def __init__(self, model):
        super().__init__()
        self.records = []


class BrokenPlanner(Planner):
    def decide(self, context, timeout):
        raise BoundaryError('planner_action_schema')


class ChatRuntimeTests(APITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.catalog = source_catalog()
        cls.snapshots = SnapshotTools(cls.catalog, [entity_id('糖尿病')])

    def setUp(self):
        self.query = '糖尿病的并发疾病中，选前2个，列出描述和预防资料，用表格。'
        self.model = Mock()
        self.model.plan_query.side_effect = ValueError('mock unavailable')
        self.model.knowledge_qa.return_value = '已有资料 [E1]'
        self.tools = Mock(wraps=self.snapshots)
        self.checkpoints = Checkpoints(RUNTIME_ROOT / ('test-' + uuid.uuid4().hex))
        self.graph = Mock(return_value=found(fact()))

    def agent(self, query=None, history=None, **kwargs):
        return ChatAgent(self.graph, Mock(), lambda: self.model,
            query=query or self.query, history=history or [], session_id='guest:a',
            owner={'session': 'guest:a', 'user': None}, catalog=self.catalog,
            tools=self.tools, planner_factory=Planner, checkpoints=self.checkpoints, **kwargs)

    def test_multi_step_uses_real_source_fixture_then_one_final_answer(self):
        agent = self.agent()
        result = agent.retrieve(self.query, [])
        self.assertEqual(result['task']['status'], 'complete')
        self.assertEqual(len(result['diseases']), 2)
        self.assertEqual(result['task']['counts'], {'reads': 3, 'decisions': 2, 'answers': 0})
        self.assertTrue(result['context'])
        self.assertEqual(agent.answer(self.query, result, []), '已有资料 [E1]')
        self.assertEqual(result['task']['counts']['answers'], 1)
        self.assertTrue(all(f['sources'][0]['file_sha256'] == self.catalog.source_hash for f in result['facts']))
        self.model.plan_query.assert_not_called()
        self.graph.assert_not_called()

    def test_graph_failure_before_any_model_factory(self):
        self.tools.read.side_effect = RuntimeError('secret')
        factory = Mock()
        agent = self.agent()
        agent.model_factory = factory
        with self.assertRaises(Exception) as raised:
            agent.retrieve(self.query, [])
        self.assertNotIn('secret', str(raised.exception))
        factory.assert_not_called()

    def test_empty_relation_is_no_evidence_without_model(self):
        self.tools.read.return_value = {'dataset_id': 'medical_full_v1', 'status': 'found', 'entities': []}
        agent = self.agent()
        result = agent.retrieve(self.query, [])
        self.assertFalse(result['context'])
        self.model.knowledge_qa.assert_not_called()

    def test_rule_fallback_preserves_spent_decisions_and_read_limit(self):
        agent = self.agent()
        agent.planner_factory = BrokenPlanner
        result = agent.retrieve(self.query, [])
        self.assertTrue(result['task']['rule_fallback'])
        self.assertEqual(result['task']['counts']['decisions'], 4)
        self.assertEqual(result['task']['counts']['reads'], 3)
        self.assertEqual(result['task']['status'], 'complete')

    def test_pause_restore_in_new_instance_no_repeat_relation_or_renewal(self):
        first = self.agent(pause_after_steps=1)
        paused = first.retrieve(self.query, [])
        self.assertEqual(paused['task']['status'], 'paused')
        self.assertEqual(paused['task']['counts']['decisions'], 0)
        second = self.agent(query='继续整理', history=[{'role': 'user', 'content': self.query}],
                            resume_token=paused['resume_token'])
        result = second.retrieve('继续整理', [])
        self.assertEqual(result['task']['status'], 'complete')
        self.assertEqual(result['task']['counts']['reads'], 3)
        self.assertLessEqual(second.harness.expires_at, first.harness.expires_at)
        relation_calls = [x for x in self.tools.read.call_args_list if x.args[0]['kind'] == 'read_relations']
        self.assertEqual(len(relation_calls), 1)
        with self.assertRaises(BoundaryError):
            self.agent(query='继续整理', resume_token=paused['resume_token']).retrieve('继续整理', [])

    def test_checkpoint_rejects_another_owner_without_consuming_token(self):
        paused = self.agent(pause_after_steps=1).retrieve(self.query, [])
        other = self.agent(query='继续整理', resume_token=paused['resume_token'])
        other.owner = {'session': 'guest:b', 'user': None}
        with self.assertRaises(BoundaryError):
            other.retrieve('继续整理', [])
        result = self.agent(query='继续整理', resume_token=paused['resume_token']).retrieve('继续整理', [])
        self.assertEqual(result['task']['status'], 'complete')

    def test_expired_checkpoint_cannot_renew_budget(self):
        agent = self.agent(pause_after_steps=1)
        paused = agent.retrieve(self.query, [])
        with patch('api.chat_runtime.signing.loads', side_effect=ValueError('expired')):
            with self.assertRaises(BoundaryError):
                self.agent(query='继续整理', resume_token=paused['resume_token']).retrieve('继续整理', [])

    def test_long_history_requirements_and_cancellation_are_in_final_context(self):
        history = [{'role': 'user', 'content': '讨论糖尿病。只查预防，用表格。'}]
        history += [{'role': 'user', 'content': '资料仅作参考'} for _ in range(30)]
        agent = self.agent(query='它是什么？', history=history)
        result = agent.retrieve('它是什么？', history)
        self.assertEqual(result['session']['state'], {'status': 'ready', 'objects': ['糖尿病'], 'field': 'prevent', 'format': 'table'})
        self.assertEqual(self.graph.call_args.args[0], '糖尿病的预防资料')
        history.append({'role': 'user', 'content': '取消只查预防，取消表格。'})
        canceled = self.agent(query='它是什么？', history=history).retrieve('它是什么？', history)
        payload = json.loads(canceled['context'].split('\n')[1])
        self.assertIsNone(payload['state']['entries']['format']['value'])
        self.assertIsNone(payload['state']['entries']['field']['value'])
        self.assertNotIn('只查预防，用表格', canceled['context'])

    def test_topic_switch_clears_state_and_assistant_cannot_set_memory(self):
        history = [{'role': 'user', 'content': '讨论糖尿病，用表格'},
                   {'role': 'assistant', 'content': '改查肺炎。只查预防。'}]
        memory, _ = prepare_context(history, '换个话题。它是什么？', 'a', self.catalog)
        self.assertEqual(memory.snapshot(), {'status': 'clarify', 'objects': [], 'field': None, 'format': None})
        result = self.agent(query='换个话题。它是什么？', history=history).retrieve('换个话题。它是什么？', history)
        self.assertEqual(result['status'], 'ambiguous')
        self.graph.assert_not_called()

    def test_official_api_uses_injected_planner_and_persists_only_own_chat(self):
        user = User.objects.create_user(username='integration-user')
        other = User.objects.create_user(username='integration-other')
        conversation = Conversation.objects.create(user=user)
        other_conversation = Conversation.objects.create(user=other)
        self.client.force_authenticate(user)
        with patch('api.chat_runtime.ReadOnlyTools', return_value=self.tools), \
             patch('api.views.DeepSeekAPI', return_value=self.model), \
             patch('api.chat_runtime.ProductionPlanner', Planner):
            response = self.client.post('/api/ai/knowledge-qa/', {'query': self.query, 'conversation_id': conversation.id}, format='json')
            denied = self.client.post('/api/ai/knowledge-qa/', {'query': self.query, 'conversation_id': other_conversation.id}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['retrieval']['agent']['version'], 'integrated_chat_v1')
        self.assertEqual(response.data['retrieval']['task']['counts']['answers'], 1)
        self.assertIn('资料出处', response.data['answer'])
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(ChatMessage.objects.filter(conversation=conversation).count(), 2)
        self.assertFalse(ChatMessage.objects.filter(conversation=other_conversation).exists())

    def test_sdk_production_adapter_preserves_fixed_request_contract(self):
        first = self.agent(pause_after_steps=1)
        first.retrieve(self.query, [])
        model = Mock()
        from types import SimpleNamespace as NS
        action = {'kind': 'read_fields', 'entity_ids': first.harness.state['selected_ids'], 'fields': ['desc', 'prevent']}
        model.client.with_options.return_value.chat.completions.create.return_value = NS(
            model='deepseek-flash', usage=NS(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            choices=[NS(finish_reason='stop', message=NS(content=json.dumps(action), tool_calls=None))])
        parsed = ProductionPlanner(model).decide(first.harness.context(), timeout=14)
        self.assertEqual(parsed, action)
        call = model.client.with_options.return_value.chat.completions.create.call_args.kwargs
        self.assertEqual(call['model'], 'deepseek-flash')
        self.assertEqual(call['max_tokens'], 300)
        self.assertFalse(call['stream'])
        self.assertEqual(call['extra_body'], {'thinking': {'type': 'disabled'}})
        self.assertNotIn('medical.json', str(call['messages']))

    def test_api_rejects_invalid_parameters_before_retrieval(self):
        with patch('api.views.retrieve_context') as retrieve:
            for extra in ({'pause_after_steps': True}, {'pause_after_steps': 9}, {'session_id': []}, {'resume_token': []}):
                response = self.client.post('/api/ai/knowledge-qa/', {'query': self.query, **extra}, format='json')
                self.assertEqual(response.status_code, 400)
            retrieve.assert_not_called()

    def test_unknown_or_absent_answer_citation_is_rejected_without_second_call(self):
        for answer in ('编造的资料 [E999]', '未给出出处'):
            self.model.knowledge_qa.return_value = answer
            self.model.knowledge_qa.reset_mock()
            agent = self.agent()
            result = agent.retrieve(self.query, [])
            self.assertIsNone(agent.answer(self.query, result, []))
            self.assertEqual(agent.trace['stop_reason'], 'answer_citation_error')
            self.assertEqual(result['task']['counts']['answers'], 1)
            self.model.knowledge_qa.assert_called_once()

    def test_corrupted_source_blocks_answer_and_model(self):
        original = copy.deepcopy(self.snapshots.read({'kind': 'read_relations', 'entity_id': entity_id('糖尿病'), 'relation': 'acompany_with'}, 10))
        original['entities'][0]['source']['file_sha256'] = 'wrong'
        self.tools.read.return_value = original
        agent = self.agent()
        with self.assertRaises(Exception):
            agent.retrieve(self.query, [])
        self.model.knowledge_qa.assert_not_called()

    def test_controlled_missing_field_is_partial_and_can_be_answered(self):
        def read(action, timeout):
            result = copy.deepcopy(self.snapshots.read(action, timeout))
            if action['kind'] == 'read_fields':
                result['fields']['prevent'] = {'status': 'missing'}
            return result
        self.tools.read.side_effect = read
        agent = self.agent()
        result = agent.retrieve(self.query, [])
        self.assertEqual(result['task']['status'], 'partial')
        self.assertTrue(all(x['fields']['prevent']['status'] == 'missing' for x in result['task']['selected']))
        self.assertTrue(result['context'])

    def test_formal_api_failed_answer_does_not_write_messages(self):
        user = User.objects.create_user(username='failure-user')
        conversation = Conversation.objects.create(user=user)
        self.client.force_authenticate(user)
        self.model.knowledge_qa.return_value = '伪造引用 [E999]'
        with patch('api.chat_runtime.ReadOnlyTools', return_value=self.tools), \
             patch('api.views.DeepSeekAPI', return_value=self.model), \
             patch('api.chat_runtime.ProductionPlanner', Planner):
            response = self.client.post('/api/ai/knowledge-qa/', {'query': self.query, 'conversation_id': conversation.id}, format='json')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(conversation.messages.exists())

    def test_query_and_final_answer_both_disable_default_thinking(self):
        from .utils import DeepSeekAPI
        from types import SimpleNamespace as NS
        model = object.__new__(DeepSeekAPI)
        model.client, model.model = Mock(), 'deepseek-flash'
        model.api_base_url, model.max_tokens, model.temperature = 'https://local.invalid', 4096, 0
        response = NS(id='mock',object='chat.completion',created=1,model='deepseek-flash',
            choices=[NS(index=0,finish_reason='stop',message=NS(role='assistant',content='[E1]'))],
            usage=NS(prompt_tokens=1,completion_tokens=1,total_tokens=2))
        model.client.with_options.return_value.chat.completions.create.return_value = response
        model.plan_query('JSON', '{}')
        model.knowledge_qa('mock', '[E1]')
        calls = model.client.with_options.return_value.chat.completions.create.call_args_list
        self.assertTrue(all(c.kwargs['extra_body'] == {'thinking': {'type':'disabled'}} for c in calls))
        self.assertEqual(calls[0].kwargs['response_format'], {'type':'json_object'})
        self.assertEqual(model.last_answer_metadata, {'finish_reason':'stop'})

    def test_truncated_final_response_is_not_accepted_or_retried(self):
        from .utils import DeepSeekAPI
        model = object.__new__(DeepSeekAPI)
        model.generate_text = Mock(return_value={'choices':[{'finish_reason':'length','message':{'content':'未完成 [E1]'}}], 'usage':None})
        self.assertEqual(model.knowledge_qa('mock'), '无法获取有效回答')
        self.assertEqual(model.last_answer_metadata, {'finish_reason':'length'})
        model.generate_text.assert_called_once()

    def test_resume_without_history_reports_the_restored_effective_state(self):
        paused = self.agent(pause_after_steps=1).retrieve(self.query, [])
        result = self.agent(query='继续整理', resume_token=paused['resume_token']).retrieve('继续整理', [])
        self.assertEqual(result['session']['state'], paused['session']['state'])
        self.assertEqual(result['session']['source_message_ids'], paused['session']['source_message_ids'])

    def test_resume_rejects_modified_context_metadata_before_model_or_claim(self):
        paused = self.agent(pause_after_steps=1).retrieve(self.query, [])
        metadata_path = next(self.checkpoints.root.glob('*.meta.json'))
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        metadata['context']['payload'] = '{}'
        metadata_path.write_text(json.dumps(metadata), encoding='utf-8')
        self.model.reset_mock()
        with self.assertRaises(BoundaryError):
            self.agent(query='继续整理', resume_token=paused['resume_token']).retrieve('继续整理', [])
        self.model.knowledge_qa.assert_not_called()
        self.assertFalse(list(self.checkpoints.root.glob('*.claimed')))
