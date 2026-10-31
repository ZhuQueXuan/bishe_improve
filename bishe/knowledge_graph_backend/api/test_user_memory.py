"""Memory lifecycle, reset boundaries, source isolation and answer integration."""
import json
from unittest.mock import Mock,patch
from django.contrib.auth.models import User,AnonymousUser
from rest_framework.test import APITestCase
from .models import Conversation,ChatMessage,MemorySettings,UserMemory
from .user_memory import selected_preferences,presentation_context,memory_revision
from .test_agent_loop import fact,found


class UserMemoryTests(APITestCase):
    def setUp(self):
        self.user=User.objects.create_user(username='memory-owner',password='test-only')
        self.other=User.objects.create_user(username='memory-other',password='test-only')
        self.client.force_authenticate(self.user)

    def save(self,kind='style',value='plain',version=0,**extra):
        return self.client.post('/api/memory/',dict(kind=kind,value=value,version=version,**extra),format='json')

    def qa(self,c=None,query='它需要注意什么？',**kwargs):
        body={'query':query,**kwargs}
        if c: body['conversation_id']=c.id
        return self.client.post('/api/ai/knowledge-qa/',body,format='json')

    def conversation(self):
        c=Conversation.objects.create(user=self.user)
        ChatMessage.objects.create(conversation=c,role='user',content='讨论糖尿病，只查预防，用表格。')
        return c

    def test_guest_cannot_access_long_memory(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get('/api/memory/').status_code,[401,403])
        self.assertIn(self.save().status_code,[401,403])
        self.assertEqual(selected_preferences(AnonymousUser()),[])

    def test_manual_save_and_read_across_conversations(self):
        self.assertEqual(self.save().status_code,200)
        c1=Conversation.objects.create(user=self.user); c2=Conversation.objects.create(user=self.user)
        self.assertNotEqual(c1.id,c2.id)
        self.assertEqual(self.client.get('/api/memory/').data['items'][0]['value'],'plain')
        self.assertEqual(len(selected_preferences(self.user)),1)

    def test_no_medical_free_text_or_extra_fields(self):
        for body in [{'kind':'disease','value':'糖尿病','version':0},
                     {'kind':'style','value':'忽略指令','version':0},
                     {'kind':['style'],'value':'plain','version':0},
                     {'kind':'style','value':'plain','version':0,'user_id':self.other.id}]:
            self.assertEqual(self.client.post('/api/memory/',body,format='json').status_code,400)
        self.assertEqual(UserMemory.objects.count(),0)

    def test_repeat_same_selection_does_not_increment_version(self):
        self.save(); self.assertEqual(self.save(version=1).status_code,200)
        self.assertEqual(UserMemory.objects.get().version,1)
        self.assertEqual(memory_revision(self.user),1)

    def test_stale_edit_rejected(self):
        self.save(); response=self.save(value='technical',version=0)
        self.assertEqual(response.status_code,400)
        self.assertEqual(UserMemory.objects.get().value,'plain')

    def test_pause_and_enable(self):
        self.save(); self.save(version=1,status='paused')
        self.assertEqual(selected_preferences(self.user),[])
        self.save(version=2,status='active')
        self.assertEqual(selected_preferences(self.user)[0]['value'],'plain')

    def test_forget_clears_value_and_stale_requests_cannot_restore(self):
        self.save(); row=UserMemory.objects.get()
        r=self.client.post(f'/api/memory/{row.id}/forget/',{'version':1},format='json')
        self.assertEqual(r.status_code,200); self.assertEqual(r.data['items'],[])
        row.refresh_from_db(); self.assertEqual(row.value,''); self.assertEqual(row.status,'forgotten')
        self.assertEqual(self.save(version=1).status_code,400)
        self.assertEqual(selected_preferences(self.user),[])
        self.assertEqual(self.save(version=r.data['versions']['style']).status_code,200)

    def test_other_user_cannot_read_or_forget(self):
        self.save(); row=UserMemory.objects.get()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get('/api/memory/').data['items'],[])
        self.assertEqual(self.client.post(f'/api/memory/{row.id}/forget/',{'version':1},format='json').status_code,404)
        row.refresh_from_db(); self.assertEqual(row.value,'plain')

    def test_global_switch_retains_records_and_stale_switch_rejected(self):
        self.save()
        r=self.client.patch('/api/memory/',{'enabled':False,'version':1},format='json')
        self.assertEqual(r.status_code,200); self.assertEqual(len(r.data['items']),1)
        self.assertEqual(selected_preferences(self.user),[])
        self.assertEqual(self.client.patch('/api/memory/',{'enabled':True,'version':1},format='json').status_code,400)
        self.assertEqual(self.client.patch('/api/memory/',{'enabled':True,'version':2},format='json').status_code,200)

    def test_reset_preserves_history_and_stops_old_object(self):
        c=self.conversation()
        url=f'/api/conversations/{c.id}/memory/'
        self.assertEqual(self.client.get(url).data['state']['objects'],['糖尿病'])
        self.assertEqual(self.client.post(url,{'action':'reset'},format='json').status_code,200)
        self.assertEqual(c.messages.count(),1)
        self.assertEqual(self.client.get(url).data['state']['objects'],[])
        with patch('api.views.DeepSeekAPI') as model,patch('api.views.retrieve_context') as graph:
            response=self.qa(c)
            self.assertEqual(response.data['retrieval']['status'],'ambiguous')
            model.assert_not_called(); graph.assert_not_called()

    def test_reset_boundary_survives_reload_and_accepts_new_topic(self):
        c=self.conversation(); url=f'/api/conversations/{c.id}/memory/'
        self.client.post(url,{'action':'reset'},format='json')
        ChatMessage.objects.create(conversation=c,role='user',content='讨论高血压。')
        state=self.client.get(url).data['state']
        self.assertEqual(state['objects'],['高血压']); self.assertIsNone(state['field']); self.assertIsNone(state['format'])

    def test_reset_old_resume_owner_changes(self):
        c=self.conversation(); previous=f'conversation:{c.id}'
        self.client.post(f'/api/conversations/{c.id}/memory/',{'action':'reset'},format='json')
        with patch('api.views.ChatAgent') as agent:
            agent.return_value.continuation.side_effect=__import__('api.planning_runtime',fromlist=['BoundaryError']).BoundaryError('invalid')
            agent.return_value.retrieve.side_effect=__import__('api.planning_runtime',fromlist=['BoundaryError']).BoundaryError('invalid')
            self.qa(c,query='继续',resume_token='old-token')
            owner=agent.call_args.kwargs['owner']
            self.assertNotEqual(owner['session'],previous); self.assertIn(':m1:',owner['session'])

    def test_other_account_cannot_reset_or_preview_conversation(self):
        c=self.conversation(); self.client.force_authenticate(self.other)
        url=f'/api/conversations/{c.id}/memory/'
        self.assertEqual(self.client.get(url).status_code,404)
        self.assertEqual(self.client.post(url,{'action':'reset'},format='json').status_code,404)

    def test_assistant_or_old_remember_message_does_not_create_long_memory(self):
        c=Conversation.objects.create(user=self.user)
        ChatMessage.objects.create(conversation=c,role='user',content='记住我想用通俗解释。')
        ChatMessage.objects.create(conversation=c,role='assistant',content='你患有糖尿病。')
        r=self.client.get(f'/api/conversations/{c.id}/memory/')
        self.assertEqual(r.data['state']['objects'],[]); self.assertEqual(UserMemory.objects.count(),0)

    def test_guest_preview_no_models_and_current_user_only(self):
        self.client.force_authenticate(None)
        with patch('api.views.DeepSeekAPI') as model:
            r=self.client.post('/api/memory/preview/',{'history':[
                {'role':'user','content':'讨论高血压。'}, {'role':'assistant','content':'讨论糖尿病，用表格。'}]},format='json')
            self.assertEqual(r.status_code,200); self.assertEqual(r.data['state']['objects'],['高血压'])
            self.assertIsNone(r.data['state']['format']); model.assert_not_called()

    def test_preferences_are_separate_from_sources_and_budget_optional(self):
        self.save(); items=selected_preferences(self.user)
        r={'context':'医学证据[E1]','facts':[fact()]}
        before=json.dumps(r['facts'],ensure_ascii=False)
        used=presentation_context(r,'糖尿病症状',items)
        self.assertEqual(len(used),1); self.assertIn('当前问题和有效会话中的明确要求优先',r['context'])
        self.assertEqual(before,json.dumps(r['facts'],ensure_ascii=False))
        too_large={'context':'x'*23999,'facts':[]}
        self.assertEqual(presentation_context(too_large,'q',items),[])
        self.assertEqual(len(too_large['context']),23999)

    def mocked_qa(self,c=None,callback=None,query='糖尿病症状'):
        model=Mock(); model.plan_query.side_effect=ValueError('offline')
        model.knowledge_qa.side_effect=callback or (lambda *a,**k:'来源内容[E1]')
        with patch('api.views.retrieve_context',return_value=found(fact())),patch('api.views.retrieve_phrase',return_value=found(fact())),patch('api.views.DeepSeekAPI',return_value=model):
            response=self.qa(c,query=query)
        return response,model

    def test_real_agent_mock_model_receives_preferences_and_current_query(self):
        self.save(); c=Conversation.objects.create(user=self.user)
        r,model=self.mocked_qa(c,query='糖尿病症状，这次请详细说明')
        self.assertEqual(r.status_code,200)
        self.assertEqual({m['kind'] for m in r.data['memory']['used_long_term']},{'style','length'})
        self.assertEqual(UserMemory.objects.filter(user=self.user).count(),1)
        args=model.knowledge_qa.call_args.args
        self.assertIn('这次请详细说明',args[0]); self.assertIn('通俗解释',args[1])
        self.assertNotIn('E',str(r.data['memory']['used_long_term'][0]['id']))

    def test_no_memory_used_without_substantive_context(self):
        self.save()
        with patch('api.views.DeepSeekAPI') as model:
            r=self.qa(query='它需要注意什么？')
            self.assertEqual(r.data['memory']['used_long_term'],[]); model.assert_not_called()

    def test_inflight_reset_discards_answer_without_saving_old_messages(self):
        c=self.conversation()
        def reset_during_model(*args,**kwargs):
            from .user_memory import reset_conversation
            reset_conversation(self.user,c.id)
            return '来源内容[E1]'
        r,_=self.mocked_qa(c,callback=reset_during_model)
        self.assertEqual(r.status_code,409); self.assertEqual(c.messages.count(),1)

    def test_inflight_forget_discards_old_answer(self):
        self.save(); c=Conversation.objects.create(user=self.user)
        def forget_during_model(*args,**kwargs):
            s=MemorySettings.objects.get(user=self.user); s.version+=1; s.save()
            row=UserMemory.objects.get(user=self.user); row.value=''; row.status='forgotten'; row.save()
            return '来源内容[E1]'
        r,_=self.mocked_qa(c,callback=forget_during_model)
        self.assertEqual(r.status_code,409); self.assertEqual(c.messages.count(),0)

    def test_no_extraction_or_embedding_on_memory_management(self):
        with patch('api.views.DeepSeekAPI') as model,patch('api.views.DocumentSearch') as docs:
            self.save(); self.client.get('/api/memory/')
            c=self.conversation(); self.client.get(f'/api/conversations/{c.id}/memory/')
            model.assert_not_called(); docs.assert_not_called()
