from unittest.mock import patch, ANY

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from .models import ChatMessage, Conversation
from .graph_retrieval import RetrievalUnavailable
from .test_agent_loop import fact, found


class ConversationApiTests(APITestCase):
    def setUp(self):
        planner = patch('api.utils.DeepSeekAPI.plan_query', side_effect=ValueError('Local planner stub'))
        planner.start()
        self.addCleanup(planner.stop)
        self.user = User.objects.create_user(username='history-user', password='test-pass-123')
        self.other_user = User.objects.create_user(username='other-user', password='test-pass-123')

    def test_conversations_require_login(self):
        response = self.client.get('/api/conversations/')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_can_create_list_and_delete_own_conversation(self):
        self.client.force_authenticate(self.user)
        create_response = self.client.post('/api/conversations/', {'title': '测试会话'}, format='json')
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)

        conversation_id = create_response.data['id']
        list_response = self.client.get('/api/conversations/')
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(list_response.data[0]['title'], '测试会话')

        delete_response = self.client.delete(f'/api/conversations/{conversation_id}/')
        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Conversation.objects.filter(id=conversation_id).exists())

    def test_user_cannot_read_another_users_conversation(self):
        conversation = Conversation.objects.create(user=self.other_user, title='他人会话')
        self.client.force_authenticate(self.user)
        response = self.client.get(f'/api/conversations/{conversation.id}/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch('api.views.DeepSeekAPI.knowledge_qa', return_value='这是测试回答 [E1]')
    @patch('api.views.retrieve_context', return_value=found(fact()))
    def test_question_and_answer_are_saved_to_the_selected_conversation(self, mock_retrieve, mock_qa):
        conversation = Conversation.objects.create(user=self.user, title='糖尿病')
        ChatMessage.objects.create(conversation=conversation, role='user', content='糖尿病是什么？')
        ChatMessage.objects.create(conversation=conversation, role='assistant', content='这是上一轮回答')
        self.client.force_authenticate(self.user)

        response = self.client.post(
            '/api/ai/knowledge-qa/',
            {'query': '它有哪些常见症状？', 'conversation_id': conversation.id},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(ChatMessage.objects.filter(conversation=conversation).count(), 4)
        self.assertEqual(response.data['conversation_title'], '糖尿病')
        mock_qa.assert_called_once_with(
            '它有哪些常见症状？',
            ANY,
            timeout=ANY,
            conversation_history=[],
        )

    @patch('api.views.DeepSeekAPI.knowledge_qa', return_value='游客多轮回答 [E1]')
    @patch('api.views.retrieve_context', return_value=found(fact()))
    def test_guest_history_is_used_without_database_persistence(self, mock_retrieve, mock_qa):
        response = self.client.post(
            '/api/ai/knowledge-qa/',
            {
                'query': '它通常需要做哪些检查？',
                'history': [
                    {'role': 'user', 'content': '糖尿病是什么？'},
                    {'role': 'assistant', 'content': '这是上一轮回答'},
                ],
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Conversation.objects.count(), 0)
        mock_qa.assert_called_once_with(
            '它通常需要做哪些检查？',
            ANY,
            timeout=ANY,
            conversation_history=[],
        )


class FullGraphQaTests(APITestCase):
    def setUp(self):
        planner = patch('api.utils.DeepSeekAPI.plan_query', side_effect=ValueError('Local planner stub'))
        planner.start()
        self.addCleanup(planner.stop)

    def test_legacy_graph_endpoints_are_removed(self):
        for method, url in [('post', '/api/nodes/'), ('patch', '/api/nodes/1/'),
                            ('delete', '/api/nodes/1/'), ('post', '/api/relationships/'),
                            ('post', '/api/ai/graph-update/')]:
            response = getattr(self.client, method)(url, {'name': 'test'}, format='json')
            self.assertEqual(response.status_code, 404)

    @patch('api.views.DeepSeekAPI')
    @patch('api.views.retrieve_context', side_effect=RetrievalUnavailable('完整图谱尚未准备好'))
    def test_unavailable_graph_does_not_generate_or_fallback(self, retrieve, model):
        response = self.client.post('/api/ai/knowledge-qa/', {'query': '糖尿病症状'}, format='json')
        self.assertEqual(response.status_code, 503)
        model.assert_not_called()

    @patch('api.views.DeepSeekAPI')
    @patch('api.views.retrieve_context', return_value={'status': 'no_evidence', 'context': '', 'facts': []})
    def test_no_evidence_is_explicit_without_model_call(self, retrieve, model):
        response = self.client.post('/api/ai/knowledge-qa/', {'query': '未知疾病'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['has_context'])
        model.return_value.knowledge_qa.assert_not_called()

    @patch('api.views.DeepSeekAPI.knowledge_qa', return_value='资料摘录显示有多尿 [E1]。')
    @patch('api.views.retrieve_context', return_value=found(fact()))
    def test_evidence_in_api_and_visible_answer(self, retrieve, qa):
        response = self.client.post('/api/ai/knowledge-qa/', {'query': '糖尿病症状'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['has_context'])
        self.assertIn('medical.json 第1行', response.data['answer'])
        self.assertNotIn('context', response.data['retrieval'])
        self.assertEqual(response.data['retrieval']['facts'][0]['evidence_id'], 'E1')

    @patch('api.views.retrieve_context')
    def test_invalid_query_never_reaches_database(self, retrieve):
        for value in ([], 1, ' ', 'x' * 4001):
            response = self.client.post('/api/ai/knowledge-qa/', {'query': value}, format='json')
            self.assertEqual(response.status_code, 400)
        retrieve.assert_not_called()
