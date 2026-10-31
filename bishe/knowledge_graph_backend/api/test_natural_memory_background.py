"""Durable review, old/new intent guards and ordinary chat with offline spies."""
import copy,json
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from .models import ChatMessage
from .natural_memory_models import NaturalMemoryLedger,NaturalMemoryBackgroundTask,NaturalMemoryInbox
from .natural_memory_contract import NaturalMemoryContractError
from . import natural_memory_background as bg,natural_memory_intake as intake
from . import test_natural_memory_answering as old_answering
from .test_natural_memory_panel import ScriptedPanelWorker
from .test_natural_memory_retrieval import evidence

class ChatProvider:
    offline_fixture=True
    def __init__(self):self.calls=[];self.purpose='general_knowledge';self.answers=[]
    def route(self,job):
        self.calls.append('route');payload=json.loads(job['messages'][-1]['content']);query=payload['sources'][-1]['text']
        personal=self.purpose=='personal_application'
        return json.dumps({'contract':'natural-memory-query-intent-v1','request_ref':job['request_ref'],'purpose':self.purpose,'subject':'self' if personal else 'none',
            'target':{'mode':'all_saved' if personal else 'none','names':[]},'time':{'scope':'current','raw':None},'current_preferences':[],'evidence':query})
    def professional(self,*args):return evidence()
    def answer(self,job):
        self.calls.append('answer');self.answers.append(copy.deepcopy(job));return json.dumps({'contract':'natural-memory-answer-v1','request_ref':job['request_ref'],
            'answer':'这是不需要先核对记忆的普通回答。','personal_refs':[],'professional_refs':['E1']})

@override_settings(ROOT_URLCONF='api.urls',NATURAL_MEMORY_ISOLATED_ENABLED=True,NATURAL_MEMORY_ISOLATED_DATABASE='memory',NATURAL_MEMORY_BACKGROUND_ENABLED=True)
class BackgroundTests(TestCase):
    setUp=old_answering.NaturalMemoryAnsweringTests.setUp
    @override_settings(NATURAL_MEMORY_BACKGROUND_ENABLED=False)
    def test_disabled_background_does_not_require_new_tables(self):
        with patch.object(bg,'cancel_locked',side_effect=AssertionError('new_tables_must_not_be_queried')):
            intake._invalidate_jobs(self.user)
    def queue(self,content='我可能有银屑病。'):
        message=ChatMessage.objects.create(conversation=self.conv,role='user',content=content)
        return bg.enqueue(self.user,message.pk)
    def process(self,content='我可能有银屑病。',worker=None):
        queued=self.queue(content);result=bg.run_task(self.user,queued['task_key'],provider=worker or ScriptedPanelWorker())
        self.assertEqual(result['status'],'needs_confirmation');return queued
    def view(self):return bg.read_state(self.user)
    def body(self,item=None,**changes):
        view=self.view();self.serial+=1;item=item or view['items'][0]
        return {'request_key':'bg-test-'+str(self.serial),'key':item['key'],'display_digest':item['display_digest'],
            'expected_revision':view['revision'],'expected_epoch':view['epoch'],'expected_policy_version':view['policy_version'],
            'decision':'accept',**changes}
    def test_queue_does_not_invoke_worker_or_save_fact(self):
        queued=self.queue();self.assertEqual(queued['status'],'queued');self.assertEqual(self.view()['entries'],[])
        self.assertEqual(NaturalMemoryBackgroundTask.objects.get().extraction_calls,0)
    def test_two_pending_items_survive_later_ordinary_chat(self):
        self.process();self.process('我去年得过湿疹。');self.queue('谢谢，请继续。')
        self.assertEqual(len(self.view()['items']),2);self.assertEqual(self.view()['entries'],[])
        first=self.view()['items'][0];self.assertEqual(len(first['later_messages']),2)
        self.assertTrue(bg.change_item(self.user,self.body(first))['saved_ack']);self.assertEqual(len(self.view()['items']),1)
    def test_new_chat_after_display_invalidates_old_click(self):
        self.process();body=self.body();self.queue('谢谢。')
        with self.assertRaisesRegex(NaturalMemoryContractError,'stale_inbox_display'):bg.change_item(self.user,body)
        self.assertTrue(bg.change_item(self.user,self.body())['saved_ack'])
    def test_many_later_messages_require_fresh_statement(self):
        self.process()
        for i in range(5):self.queue('另一条消息'+str(i))
        self.assertEqual(self.view()['items'][0]['status'],'needs_context')
        with self.assertRaisesRegex(NaturalMemoryContractError,'fresh_statement_required'):bg.change_item(self.user,self.body())
    def test_confirm_preserves_uncertainty_and_replay_is_unique(self):
        self.process();body=self.body();result=bg.change_item(self.user,body);self.assertTrue(result['saved_ack'])
        self.assertEqual(bg.change_item(self.user,body)['status'],'replayed')
        entry=self.view()['entries'][0];self.assertEqual(entry['fact']['content']['certainty'],'possible');self.assertFalse(entry['clinical_confirmation'])
        self.assertEqual(NaturalMemoryInbox.objects.get().payload,{})
    def test_reject_does_not_save(self):
        self.process();bg.change_item(self.user,self.body(decision='reject'));self.assertEqual(self.view()['entries'],[])
    def test_policy_change_clears_pending_payload_and_queued_work(self):
        self.process();self.queue('以后回答简短一些。');row=NaturalMemoryLedger.objects.get(user=self.user)
        intake.configure(self.user,expected_epoch=row.epoch,expected_policy_version=row.policy['version'],collect_types=[],use_enabled=False,cloud_extraction=False,cloud_verification=False)
        self.assertEqual(self.view()['items'],[]);self.assertFalse(NaturalMemoryInbox.objects.exclude(payload={}).exists())
        self.assertEqual(NaturalMemoryBackgroundTask.objects.filter(status='queued').count(),0)
    def test_source_reset_prevents_processing(self):
        queued=self.queue();self.conv.memory_version+=1;self.conv.save(update_fields=['memory_version']);worker=ScriptedPanelWorker()
        self.assertEqual(bg.run_task(self.user,queued['task_key'],provider=worker)['status'],'processing_failed');self.assertEqual(worker.extract_calls,0)
    def test_expired_task_does_not_call(self):
        queued=self.queue();NaturalMemoryBackgroundTask.objects.update(deadline_at=timezone.now()-timedelta(seconds=1));worker=ScriptedPanelWorker()
        self.assertEqual(bg.run_task(self.user,queued['task_key'],provider=worker)['status'],'processing_failed');self.assertEqual(worker.extract_calls,0)
    def test_running_or_completed_task_never_retries(self):
        queued=self.queue();NaturalMemoryBackgroundTask.objects.update(status='running',extraction_calls=1);worker=ScriptedPanelWorker()
        self.assertEqual(bg.run_task(self.user,queued['task_key'],provider=worker)['new_calls'],0);self.assertEqual(worker.extract_calls,0)
    def test_late_model_reply_is_not_saved(self):
        queued=self.queue();worker=ScriptedPanelWorker();original=worker.extract
        def late(request):
            raw=original(request);NaturalMemoryBackgroundTask.objects.update(deadline_at=timezone.now()-timedelta(seconds=1));return raw
        worker.extract=late;self.assertEqual(bg.run_task(self.user,queued['task_key'],provider=worker)['status'],'processing_failed');self.assertEqual(self.view()['entries'],[])
    def test_forget_clears_affected_payload_but_keeps_unrelated_pending(self):
        self.process();bg.change_item(self.user,self.body());self.process('我去年得过湿疹。');view=self.view();entry=view['entries'][0]
        original_id=int(entry['sources'][0]['message_id'][3:])
        message=ChatMessage.objects.create(conversation=self.conv,role='user',content='我可能有银屑病。')
        queued=bg.enqueue(self.user,message.pk,context_ids=[original_id])
        self.assertEqual(bg.run_task(self.user,queued['task_key'],provider=ScriptedPanelWorker())['status'],'needs_clarification')
        entry=self.view()['entries'][0]
        row=NaturalMemoryLedger.objects.get(user=self.user);intake.manage_entry(self.user,{'request_key':'forget-bg','conversation_id':self.conv.pk,'target':entry['key'],
            'target_version':entry['version'],'display_digest':entry['display_digest'],'action':'forget','expected_revision':row.revision,'expected_epoch':row.epoch,'expected_policy_version':row.policy['version']})
        self.assertEqual(len(self.view()['items']),1);self.assertIn('湿疹',self.view()['items'][0]['source_text'])
        self.assertEqual(NaturalMemoryInbox.objects.filter(status='stale',payload={}).count(),1)
    def test_foreign_account_cannot_see_or_confirm(self):
        self.process();body=self.body();intake.initialize_account(self.other)
        self.assertEqual(bg.read_state(self.other)['items'],[])
        body.update(expected_epoch=0,expected_revision=0,expected_policy_version=1)
        with self.assertRaisesRegex(NaturalMemoryContractError,'inbox_unavailable'):bg.change_item(self.other,body)
    def test_read_does_not_create_or_write(self):
        self.process();before=NaturalMemoryLedger.objects.get(user=self.user).lock_version;self.view()
        self.assertEqual(NaturalMemoryLedger.objects.get(user=self.user).lock_version,before)
        self.assertEqual(bg.read_state(self.other)['status'],'unconfigured');self.assertFalse(NaturalMemoryLedger.objects.filter(user=self.other).exists())
    def test_reply_is_queued_and_never_calls_model_inline(self):
        self.process();body=self.body();body.pop('decision');body.update(content='我说的是自己，仍没有确定。',relation=None)
        self.assertEqual(bg.revise_item(self.user,body)['status'],'reply_received');self.assertEqual(NaturalMemoryInbox.objects.get().status,'replaced')
        self.assertEqual(NaturalMemoryBackgroundTask.objects.filter(status='queued').count(),1)
    def test_wrong_source_or_target_choice_is_not_accepted(self):
        self.process();body=self.body();body.pop('decision');body.update(content=None,relation={'operation':'correction','target':'wrong','target_version':1})
        with self.assertRaises(NaturalMemoryContractError):bg.revise_item(self.user,body)
        self.assertEqual(self.view()['entries'],[])
    def test_api_does_not_accept_raw_candidates_or_trusted_flags(self):
        self.process();body=self.body();body['allow_later_messages']=True
        self.assertEqual(self.client.post('/natural-memory/decision/',body,format='json').status_code,409)
        self.assertEqual(APIClient().get('/natural-memory/inbox/').status_code,401)
    def test_disabled_route_is_closed(self):
        with override_settings(NATURAL_MEMORY_BACKGROUND_ENABLED=False):
            self.assertEqual(self.client.get('/natural-memory/inbox/').status_code,404)
            self.assertFalse(self.client.get('/natural-memory/availability/').data['available'])
    def test_normal_chat_answers_without_running_collection(self):
        provider=ChatProvider();worker=ScriptedPanelWorker()
        with override_settings(NATURAL_MEMORY_CHAT_PROVIDER=provider,NATURAL_MEMORY_BACKGROUND_PROVIDER=worker):
            response=self.client.post('/ai/knowledge-qa/',{'query':'请解释资料的边界。','conversation_id':self.conv.pk,'client_message_id':'first'},format='json')
        self.assertEqual(response.status_code,200,response.data);self.assertIn('普通回答',response.data['answer'])
        self.assertFalse(response.data['memory']['natural_memory']['confirmation_required_to_continue_chat'])
        self.assertEqual(worker.extract_calls,0);self.assertEqual(NaturalMemoryBackgroundTask.objects.filter(status='queued').count(),1)
    def test_collection_storage_failure_does_not_fail_chat(self):
        with override_settings(NATURAL_MEMORY_CHAT_PROVIDER=ChatProvider()),patch.object(bg,'enqueue',side_effect=RuntimeError('synthetic queue failure')):
            response=self.client.post('/ai/knowledge-qa/',{'query':'继续解释。','conversation_id':self.conv.pk,'client_message_id':'fail-queue'},format='json')
        self.assertEqual(response.status_code,200,response.data);self.assertEqual(ChatMessage.objects.filter(role='assistant').count(),1)
    def test_pending_review_does_not_block_next_normal_answer(self):
        self.process();first_key=self.view()['items'][0]['key']
        with override_settings(NATURAL_MEMORY_CHAT_PROVIDER=ChatProvider()):
            response=self.client.post('/ai/knowledge-qa/',{'query':'我继续问一般知识。','conversation_id':self.conv.pk,'client_message_id':'while-pending'},format='json')
        self.assertEqual(response.status_code,200,response.data);self.assertEqual(self.view()['items'][0]['key'],first_key)
    def test_duplicate_normal_message_never_duplicates_model_calls(self):
        provider=ChatProvider();body={'query':'解释资料。','conversation_id':self.conv.pk,'client_message_id':'same-message'}
        with override_settings(NATURAL_MEMORY_CHAT_PROVIDER=provider):
            self.assertEqual(self.client.post('/ai/knowledge-qa/',body,format='json').status_code,200)
            self.assertEqual(self.client.post('/ai/knowledge-qa/',body,format='json').status_code,409)
        self.assertEqual(provider.calls,['route','answer']);self.assertEqual(NaturalMemoryBackgroundTask.objects.count(),1)
    def test_formal_gate_and_database_binding_cannot_be_opened_by_browser(self):
        with override_settings(NATURAL_MEMORY_ISOLATED_ENABLED=False,NATURAL_MEMORY_FORMAL_ENABLED=True,NATURAL_MEMORY_RELEASE_READY=False):
            self.assertEqual(self.client.get('/natural-memory/inbox/').status_code,404)
        with override_settings(NATURAL_MEMORY_ISOLATED_ENABLED=False,NATURAL_MEMORY_FORMAL_ENABLED=True,NATURAL_MEMORY_RELEASE_READY=True,NATURAL_MEMORY_FORMAL_DATABASE='wrong'):
            self.assertEqual(self.client.get('/natural-memory/inbox/').status_code,409)
    def test_no_personal_answer_permission_still_allows_general_answer(self):
        self.process();bg.change_item(self.user,self.body())
        from . import natural_memory_answering as answering
        row=NaturalMemoryLedger.objects.get(user=self.user);grant=answering.grants(self.user)
        answering.configure_grants(self.user,expected_version=grant['version'],expected_epoch=row.epoch,cloud_routing=True,cloud_answer_personal_context=False)
        provider=ChatProvider();provider.purpose='personal_application'
        with override_settings(NATURAL_MEMORY_CHAT_PROVIDER=provider):
            response=self.client.post('/ai/knowledge-qa/',{'query':'结合我的情况解释资料。','conversation_id':self.conv.pk,'client_message_id':'no-private-context'},format='json')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(provider.answers[0]['payload']['personal_reports'],[])
        self.assertTrue(provider.answers[0]['payload']['limitations']['saved_background_omitted_for_permissions'])
    def test_normal_pause_resume_preserves_budget_and_has_no_phantom_user_message(self):
        provider=ChatProvider();body={'query':'解释资料。','conversation_id':self.conv.pk,'client_message_id':'pause-chat','pause_after_steps':1}
        with override_settings(NATURAL_MEMORY_CHAT_PROVIDER=provider):
            paused=self.client.post('/ai/knowledge-qa/',body,format='json');self.assertEqual(paused.status_code,200,paused.data)
            self.assertEqual(provider.calls,['route']);token=paused.data['retrieval']['resume_token']
            resumed=self.client.post('/ai/knowledge-qa/',{'query':'继续整理','conversation_id':self.conv.pk,'client_message_id':'resume-chat','resume_token':token},format='json')
            self.assertEqual(resumed.status_code,200,resumed.data);self.assertNotIn('user_message',resumed.data)
            self.assertEqual(provider.calls,['route','answer']);self.assertEqual(ChatMessage.objects.filter(role='user').count(),1)
            again=self.client.post('/ai/knowledge-qa/',{'query':'继续整理','conversation_id':self.conv.pk,'client_message_id':'resume-again','resume_token':token},format='json')
            self.assertEqual(again.status_code,409);self.assertEqual(provider.calls,['route','answer'])
    def test_normal_checkpoint_cannot_be_delivered_to_another_conversation(self):
        from .models import Conversation
        provider=ChatProvider()
        with override_settings(NATURAL_MEMORY_CHAT_PROVIDER=provider):
            paused=self.client.post('/ai/knowledge-qa/',{'query':'解释资料。','conversation_id':self.conv.pk,'client_message_id':'pause-other','pause_after_steps':1},format='json')
            other=Conversation.objects.create(user=self.user)
            denied=self.client.post('/ai/knowledge-qa/',{'query':'继续整理','conversation_id':other.pk,'client_message_id':'other-session','resume_token':paused.data['retrieval']['resume_token']},format='json')
            self.assertEqual(denied.status_code,409);self.assertEqual(provider.calls,['route'])
    def test_permission_change_blocks_old_normal_checkpoint_without_extra_calls(self):
        provider=ChatProvider()
        with override_settings(NATURAL_MEMORY_CHAT_PROVIDER=provider):
            paused=self.client.post('/ai/knowledge-qa/',{'query':'解释资料。','conversation_id':self.conv.pk,'client_message_id':'pause-stale','pause_after_steps':1},format='json')
            row=NaturalMemoryLedger.objects.get(user=self.user)
            intake.configure(self.user,expected_epoch=row.epoch,expected_policy_version=row.policy['version'],collect_types=[],use_enabled=False,cloud_extraction=False,cloud_verification=False)
            denied=self.client.post('/ai/knowledge-qa/',{'query':'继续整理','conversation_id':self.conv.pk,'client_message_id':'stale-resume','resume_token':paused.data['retrieval']['resume_token']},format='json')
            self.assertEqual(denied.status_code,409);self.assertEqual(provider.calls,['route'])
    def test_completed_audits_are_not_a_lifetime_chat_limit(self):
        from .natural_memory_models import NaturalMemoryOperation
        NaturalMemoryOperation.objects.bulk_create([NaturalMemoryOperation(user=self.user,request_key='old-'+str(i),input_hash='h',receipt={'status':'answered'}) for i in range(300)])
        with override_settings(NATURAL_MEMORY_CHAT_PROVIDER=ChatProvider()):
            response=self.client.post('/ai/knowledge-qa/',{'query':'解释资料。','conversation_id':self.conv.pk,'client_message_id':'after-many-audits'},format='json')
        self.assertEqual(response.status_code,200,response.data)
