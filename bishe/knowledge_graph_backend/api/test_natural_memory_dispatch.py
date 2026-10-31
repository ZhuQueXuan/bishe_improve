"""Bounded real-adapter code exercised with offline transports only."""
import json,subprocess
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.utils import timezone
from . import test_natural_memory_answering as fixtures
from . import natural_memory_dispatch as dispatch,natural_memory_background as bg
from .natural_memory_dispatch import BoundedModelProvider as Provider
from .natural_memory_models import NaturalMemoryDispatchBudget as Budget,NaturalMemoryDispatchAttempt as Attempt,NaturalMemoryBackgroundTask
from .natural_memory_contract import NaturalMemoryContractError,check_model_messages_budget
from .models import ChatMessage
from .test_natural_memory_panel import ScriptedPanelWorker
from .test_natural_memory_background import ChatProvider
from .test_natural_memory_retrieval import evidence

@override_settings(ROOT_URLCONF='api.urls',NATURAL_MEMORY_ISOLATED_ENABLED=True,NATURAL_MEMORY_ISOLATED_DATABASE='memory',
    NATURAL_MEMORY_BACKGROUND_ENABLED=True,NATURAL_MEMORY_DISPATCH_CANDIDATE_ENABLED=True,NATURAL_MEMORY_LIVE_DISPATCH_ENABLED=False)
class DispatchTests(TestCase):
    setUp=fixtures.NaturalMemoryAnsweringTests.setUp
    def provider(self,**changes):
        if not Budget.objects.exists():dispatch.approve_budget(batch_key='new-approved',authorization_hash='a'*64,owner_ids=[self.user.pk],
            stages=sorted(dispatch.STAGES),maximum_calls=8,budget_cny=1,expires_at=timezone.now()+timedelta(minutes=10))
        self.sent=[];self.events=[]
        def transport(messages,**options):
            self.sent.append({'messages':messages,**options});return {'content':'{"ok":true}','model':'deepseek-flash','finish_reason':'stop',
                'usage':{'prompt_tokens':100,'completion_tokens':20}}
        options={'counter':lambda m:100,'professional':lambda *a:evidence(),'audit_sink':self.events.append,'verify_frozen':lambda:True,
            'owner_id':self.user.pk,'transport':transport,**changes}
        return Provider('new-approved',**options)
    def request(self,ref='request-1'):
        messages=[{'role':'system','content':'虚构边界测试'},{'role':'user','content':'不含真实个人信息'}]
        return {'request_ref':ref,'messages':messages,'input_bytes':check_model_messages_budget(messages),
            'remaining_original_seconds':60,'constraints':{'timeout_seconds_cap':45}}
    def reject(self,provider,code,request=None):
        with self.assertRaisesRegex(NaturalMemoryContractError,code):provider.route(request or self.request())
    def test_success_reserves_and_records_known_usage(self):
        provider=self.provider();self.assertEqual(provider.route(self.request()),'{"ok":true}')
        budget=Budget.objects.get();self.assertEqual(budget.attempts,1);self.assertEqual(budget.estimated_usd,Decimal('.000054'))
        self.assertEqual(budget.reserved_usd,0);self.assertEqual(Attempt.objects.get().status,'completed');self.assertEqual(len(self.sent),1)
    def test_disabled_gate_never_touches_transport(self):
        provider=self.provider()
        with override_settings(NATURAL_MEMORY_DISPATCH_CANDIDATE_ENABLED=False):self.reject(provider,'dispatch_disabled')
        self.assertEqual(self.sent,[]);self.assertFalse(Attempt.objects.exists())
    def test_real_gate_closed_never_reads_credentials(self):
        provider=self.provider(transport=None)
        with patch.object(dispatch,'request_once',side_effect=AssertionError('no real request')):self.reject(provider,'live_dispatch_disabled')
        self.assertFalse(Attempt.objects.exists())
    def test_unapproved_or_closed_budget_never_sends(self):
        provider=self.provider();Budget.objects.update(approved=False);self.reject(provider,'dispatch_closed_or_expired');self.assertFalse(self.sent)
    def test_foreign_user_cannot_bind_adapter(self):
        provider=self.provider()
        with self.assertRaisesRegex(NaturalMemoryContractError,'dispatch_owner'):provider.for_user(self.other)
        self.assertFalse(self.sent)
    def test_count_limit_no_dispatch(self):
        provider=self.provider();Budget.objects.update(attempts=8);self.reject(provider,'dispatch_budget_or_call_limit');self.assertFalse(self.sent)
    def test_budget_reserves_in_flight_cost(self):
        provider=self.provider();Budget.objects.update(reserved_usd=Decimal('.124'));self.reject(provider,'dispatch_budget_or_call_limit');self.assertFalse(self.sent)
    def test_expired_budget_does_not_send(self):
        provider=self.provider();Budget.objects.update(expires_at=timezone.now()-timedelta(seconds=1));self.reject(provider,'dispatch_closed_or_expired');self.assertFalse(self.sent)
    def test_duplicate_request_never_retries(self):
        provider=self.provider();provider.route(self.request());self.reject(provider,'dispatch_already_attempted');self.assertEqual(len(self.sent),1)
    def test_scope_denies_unapproved_stage(self):
        provider=self.provider();Budget.objects.update(stages=['extraction']);self.reject(provider,'dispatch_scope');self.assertFalse(self.sent)
    def test_unknown_usage_closes_and_retains_reservation(self):
        provider=self.provider(transport=lambda *a,**k:{'usage':None});self.reject(provider,'dispatch_transport_or_unknown_usage')
        budget=Budget.objects.get();self.assertTrue(budget.closed);self.assertEqual(budget.reserved_usd,dispatch.RESERVE)
        self.assertEqual(Attempt.objects.get().status,'unknown')
    def test_transport_timeout_never_retries(self):
        def timeout(*a,**k):raise TimeoutError('synthetic')
        provider=self.provider(transport=timeout);self.reject(provider,'dispatch_transport_or_unknown_usage');self.assertEqual(Budget.objects.get().attempts,1)
        self.assertEqual(self.events[-1]['event'],'unknown_outcome')
    def test_known_truncation_is_charged_before_close(self):
        provider=self.provider();send=provider.transport
        provider.transport=lambda *a,**k:{**send(*a,**k),'finish_reason':'length'}
        self.reject(provider,'dispatch_incomplete');self.assertEqual(Budget.objects.get().estimated_usd,Decimal('.000054'))
    def test_wrong_model_stops_with_known_cost(self):
        provider=self.provider();send=provider.transport;provider.transport=lambda *a,**k:{**send(*a,**k),'model':'other'}
        self.reject(provider,'dispatch_model_changed');self.assertTrue(Budget.objects.get().closed)
    def test_over_reserve_usage_stops(self):
        provider=self.provider();send=provider.transport;provider.transport=lambda *a,**k:{**send(*a,**k),'usage':{'prompt_tokens':15001,'completion_tokens':20}}
        self.reject(provider,'dispatch_usage_exceeded')
    def test_unknown_old_attempt_blocks_new_sends(self):
        provider=self.provider();budget=Budget.objects.get()
        Attempt.objects.create(budget=budget,owner_id=self.user.pk,stage='answer',request_ref='lost',request_hash='a'*64,
            deadline_at=timezone.now()-timedelta(seconds=1))
        self.reject(provider,'dispatch_interrupted_unknown');self.assertFalse(self.sent)
    def test_input_counter_and_complete_bytes_checked(self):
        provider=self.provider(counter=lambda m:7000);self.reject(provider,'dispatch_input_reserve');self.assertFalse(self.sent)
    def test_freeze_change_prevents_any_send(self):
        provider=self.provider(verify_frozen=lambda:False);self.reject(provider,'dispatch_freeze_changed');self.assertFalse(self.sent)
    def test_deadline_not_restarted(self):
        provider=self.provider();request=self.request();request['remaining_original_seconds']=0;self.reject(provider,'dispatch_deadline',request);self.assertFalse(self.sent)
    def test_missing_audit_prevents_outbound(self):
        def fail(event):raise OSError('synthetic disk full')
        provider=self.provider(audit_sink=fail);self.reject(provider,'dispatch_audit_failed');self.assertFalse(self.sent)
        self.assertEqual(Attempt.objects.get().status,'not_dispatched');self.assertEqual(Budget.objects.get().reserved_usd,0)
    def test_database_audit_does_not_store_prompt_or_response_body(self):
        provider=self.provider();provider.route(self.request());fields=Attempt.objects.get().__dict__
        self.assertNotIn('不含真实个人信息',str(fields));self.assertNotIn('{"ok":true}',str(fields));self.assertEqual(len(fields['request_hash']),64)
    def test_background_worker_uses_bounded_adapter_and_keeps_health_pending(self):
        worker=ScriptedPanelWorker()
        def transport(messages,**options):
            payload=json.loads(messages[-1]['content']);job={'messages':messages,'request_ref':payload['request_ref']}
            raw=worker.review(job) if 'extraction_outcome' in payload else worker.extract(job)
            return {'content':raw,'model':'deepseek-flash','finish_reason':'stop','usage':{'prompt_tokens':100,'completion_tokens':20}}
        provider=self.provider(transport=transport);msg=ChatMessage.objects.create(conversation=self.conv,role='user',content='我可能有银屑病。')
        queued=bg.enqueue(self.user,msg.pk);result=bg.run_task(self.user,queued['task_key'],provider=provider)
        self.assertEqual(result['status'],'needs_confirmation',list(self.events));self.assertEqual(len(bg.read_state(self.user)['items']),1)
        self.assertFalse(bg.read_state(self.user)['entries']);self.assertEqual(Budget.objects.get().attempts,2)
    def test_normal_chat_bounded_adapter_does_not_run_background_inline(self):
        scripted=ChatProvider()
        def transport(messages,**options):
            payload=json.loads(messages[-1]['content']);job={'messages':messages,'request_ref':payload['request_ref'],'payload':payload}
            raw=scripted.route(job) if 'sources' in payload else scripted.answer(job)
            return {'content':raw,'model':'deepseek-flash','finish_reason':'stop','usage':{'prompt_tokens':100,'completion_tokens':20}}
        provider=self.provider(transport=transport)
        with override_settings(NATURAL_MEMORY_CHAT_PROVIDER=provider):
            response=self.client.post('/ai/knowledge-qa/',{'query':'解释资料。','conversation_id':self.conv.pk,'client_message_id':'new'},format='json')
        self.assertEqual(response.status_code,200,response.data);self.assertEqual(Budget.objects.get().attempts,2)
        self.assertEqual(NaturalMemoryBackgroundTask.objects.get().status,'queued')
    def test_downstream_bad_json_closes_batch_without_a_second_stage(self):
        provider=self.provider(transport=lambda *a,**k:{'content':'bad JSON','model':'deepseek-flash','finish_reason':'stop','usage':{'prompt_tokens':100,'completion_tokens':20}})
        msg=ChatMessage.objects.create(conversation=self.conv,role='user',content='我可能有银屑病。');queued=bg.enqueue(self.user,msg.pk)
        self.assertEqual(bg.run_task(self.user,queued['task_key'],provider=provider)['status'],'processing_failed')
        budget=Budget.objects.get();self.assertTrue(budget.closed);self.assertEqual(budget.attempts,1)
    def test_closed_normal_provider_does_not_fall_back_to_unbounded_model(self):
        provider=self.provider();Budget.objects.update(closed=True)
        with override_settings(NATURAL_MEMORY_CHAT_PROVIDER=provider):
            response=self.client.post('/ai/knowledge-qa/',{'query':'解释资料。','conversation_id':self.conv.pk,'client_message_id':'blocked'},format='json')
        self.assertEqual(response.status_code,503);self.assertFalse(self.sent)
    def test_background_preference_change_confirm_undo_and_stale_replay(self):
        from . import natural_memory_intake as intake
        from .natural_memory_models import NaturalMemoryLedger
        row=NaturalMemoryLedger.objects.get(user=self.user)
        intake.configure(self.user,expected_epoch=row.epoch,expected_policy_version=row.policy['version'],collect_types=row.policy['collect_types'],use_enabled=True,
            cloud_extraction=True,cloud_verification=True,preference_auto_enabled=True,preference_auto_gate_passed=True)
        class PrefWorker:
            offline_fixture=True
            def extract(self,request):
                payload=json.loads(request['messages'][-1]['content']);text=payload['sources'][-1]['content']
                value='detailed' if '详细' in text else 'brief'
                return json.dumps({'contract':'natural-memory-extraction-v1','request_ref':request['request_ref'],'outcome':'propose','clarifications':[],
                    'items':[{'contract':'natural-memory-candidate-v1','category':'presentation_preference','subject':'self','content':{'slot':'length','value':value,'scope':'long_term'},
                     'time':{'raw':None,'temporal_scope':'not_applicable'},'relation':'new_information','evidence':[{'source_ref':'current','text':text}]}]},ensure_ascii=False)
        for text in ['以后回答简短一些。','以后回答详细一些。']:
            message=ChatMessage.objects.create(conversation=self.conv,role='user',content=text);queued=bg.enqueue(self.user,message.pk)
            bg.run_task(self.user,queued['task_key'],provider=PrefWorker())
        view=bg.read_state(self.user);item=view['items'][0];entry=view['entries'][0]
        body={'request_key':'choose-new-default','expected_revision':view['revision'],'expected_epoch':view['epoch'],'expected_policy_version':view['policy_version'],
            'key':item['key'],'display_digest':item['display_digest'],'content':None,'relation':{'operation':'new_default','target':entry['key'],'target_version':entry['version']}}
        bg.revise_item(self.user,body);view=bg.read_state(self.user);item=view['items'][0]
        bg.change_item(self.user,{'request_key':'confirm-default','expected_revision':view['revision'],'expected_epoch':view['epoch'],'expected_policy_version':view['policy_version'],
            'key':item['key'],'display_digest':item['display_digest'],'decision':'accept'})
        view=bg.read_state(self.user);entry=view['entries'][0];self.assertTrue(entry['can_undo']);self.assertEqual(entry['fact']['content']['value'],'detailed')
        undo={'request_key':'undo-default','conversation_id':self.conv.pk,'target':entry['key'],'target_version':entry['version'],'display_digest':entry['display_digest'],
            'action':'undo','expected_revision':view['revision'],'expected_epoch':view['epoch'],'expected_policy_version':view['policy_version']}
        result=intake.manage_entry(self.user,undo);view=bg.read_state(self.user);self.assertEqual(view['entries'][0]['fact']['content']['value'],'brief')
        self.assertGreater(view['entries'][0]['version'],entry['version']);self.assertEqual(intake.manage_entry(self.user,undo)['status'],'replayed')
        entry=view['entries'][0];forget={**undo,'request_key':'forget-pref','action':'forget','target_version':entry['version'],'display_digest':entry['display_digest'],
            'expected_revision':view['revision'],'expected_epoch':view['epoch']}
        intake.manage_entry(self.user,forget);self.assertEqual(intake.manage_entry(self.user,undo)['status'],'replayed')
        self.assertIsNone(bg.read_state(self.user)['entries'][0]['fact'])
    def test_existing_sdk_sends_once_to_mock_http_transport(self):
        import httpx2 as httpx
        from openai import OpenAI
        from .natural_memory_dispatch_transport import sdk_response
        received=[]
        def handler(request):
            received.append(json.loads(request.content));return httpx.Response(200,json={'id':'synthetic','object':'chat.completion','created':0,'model':'deepseek-flash',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'{}'}}],
                'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}})
        options=[]
        def factory(**kw):options.append(kw);return OpenAI(**kw,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'synthetic-not-real'}):result=sdk_response({'messages':self.request()['messages'],'max_tokens':1024,'timeout':1},factory=factory)
        self.assertEqual(len(received),1);self.assertEqual(options[0]['max_retries'],0);self.assertEqual(options[0]['base_url'],'https://api.deepseek.com')
        self.assertEqual(received[0]['thinking'],{'type':'disabled'});self.assertEqual(result['usage']['prompt_tokens'],100)
    def test_persisted_critical_conflict_can_be_reopened_and_resolved(self):
        from .test_natural_memory_extraction import review
        from .natural_memory_models import NaturalMemoryLedger,NaturalMemoryBackgroundTask
        class Worker:
            offline_fixture=True
            def extract(self,request):
                payload=json.loads(request['messages'][-1]['content']);text=payload['sources'][-1]['content'];negative='没有' in text
                return json.dumps({'contract':'natural-memory-extraction-v1','request_ref':request['request_ref'],'outcome':'propose','clarifications':[],
                    'items':[{'contract':'natural-memory-candidate-v1','category':'self_disease_report','subject':'self',
                    'content':{'value_raw':'示例病','polarity':'negated' if negative else 'affirmed','certainty':'possible' if negative else 'asserted'},
                    'time':{'raw':None,'temporal_scope':'unspecified'},'relation':'new_information','evidence':[{'source_ref':'current','text':text}]}]},ensure_ascii=False)
            def review(worker,request):
                payload=json.loads(request['messages'][-1]['content']);source=NaturalMemoryBackgroundTask.objects.filter(user=self.user,status='running').latest('pk').snapshot
                return review(source,worker.extract({'messages':[{}, {'content':json.dumps({'sources':payload['sources']})}],'request_ref':'unused'}))
        worker=Worker()
        # Generic model fixture: review against the exact extraction returned
        # by the running task, rather than a second synthetic extraction ref.
        def reviewed(request):
            task=NaturalMemoryBackgroundTask.objects.filter(user=self.user,status='running').latest('pk')
            return review(task.snapshot,task.extraction_reply)
        worker.review=reviewed
        def process(text):
            msg=ChatMessage.objects.create(conversation=self.conv,role='user',content=text);queued=bg.enqueue(self.user,msg.pk)
            return bg.run_task(self.user,queued['task_key'],provider=worker)
        process('我有示例病。');view=bg.read_state(self.user);item=view['items'][0]
        bg.change_item(self.user,{'request_key':'seed-health-confirm','expected_revision':view['revision'],'expected_epoch':view['epoch'],'expected_policy_version':view['policy_version'],
            'key':item['key'],'display_digest':item['display_digest'],'decision':'accept'})
        self.assertEqual(process('我可能没有示例病。')['status'],'needs_clarification')
        state=NaturalMemoryLedger.objects.get(user=self.user).state;view=bg.read_state(self.user);again=bg.read_state(self.user)
        self.assertEqual(view,again);self.assertEqual(NaturalMemoryLedger.objects.get(user=self.user).state,state)
        item=view['items'][0];entry=view['entries'][0];self.assertNotEqual(item['status'],'unavailable')
        bg.revise_item(self.user,{'request_key':'choose-conflict-correction','expected_revision':view['revision'],'expected_epoch':view['epoch'],'expected_policy_version':view['policy_version'],
            'key':item['key'],'display_digest':item['display_digest'],'content':None,'relation':{'operation':'correction','target':entry['key'],'target_version':entry['version']}})
        view=bg.read_state(self.user);item=view['items'][0]
        bg.change_item(self.user,{'request_key':'confirm-correction','expected_revision':view['revision'],'expected_epoch':view['epoch'],'expected_policy_version':view['policy_version'],
            'key':item['key'],'display_digest':item['display_digest'],'decision':'accept'})
        content=bg.read_state(self.user)['entries'][0]['fact']['content'];self.assertEqual(content['polarity'],'negated');self.assertEqual(content['certainty'],'possible')
    def test_hard_wall_timeout_cannot_start_a_retry(self):
        from .natural_memory_dispatch_transport import request_once
        with patch('subprocess.run',side_effect=subprocess.TimeoutExpired('synthetic',1)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):request_once(self.request()['messages'],max_tokens=1024,timeout=1)
        self.assertEqual(run.call_count,1);self.assertEqual(run.call_args.kwargs['timeout'],1)
    def test_existing_sdk_timeout_does_not_send_twice(self):
        import httpx2 as httpx
        from openai import OpenAI
        from .natural_memory_dispatch_transport import sdk_response
        requests=[]
        def handler(request):requests.append(request);raise httpx.ReadTimeout('synthetic timeout',request=request)
        def factory(**kw):return OpenAI(**kw,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'synthetic-not-real'}):
            with self.assertRaises(Exception):sdk_response({'messages':self.request()['messages'],'max_tokens':1024,'timeout':1},factory=factory)
        self.assertEqual(len(requests),1)
    def test_real_sdk_rejects_offline_placeholder_before_client_creation(self):
        from .natural_memory_dispatch_transport import sdk_response
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'offline-placeholder'}),patch('openai.OpenAI',side_effect=AssertionError('no real client')) as factory:
            with self.assertRaisesRegex(RuntimeError,'placeholder_credential_rejected'):
                sdk_response({'messages':self.request()['messages'],'max_tokens':1024,'timeout':1})
        self.assertEqual(factory.call_count,0)
