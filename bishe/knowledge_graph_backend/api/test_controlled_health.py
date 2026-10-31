import copy
import json
from datetime import timedelta
from unittest.mock import Mock,patch
from django.contrib.auth.models import User
from django.db import OperationalError
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from .models import Conversation,ChatMessage,MemorySettings,HealthMemory,HealthMemoryProposal
from .controlled_health import parse_command,stored_report,strip_control_history
from .test_chat_runtime import found,fact


@override_settings(DOCUMENT_RAG_ENABLED=False)
class ControlledHealthTests(APITestCase):
    def setUp(self):
        self.u=User.objects.create_user(username='controlled-health-synthetic')
        self.c=Conversation.objects.create(user=self.u)
        self.client.force_authenticate(self.u);self.n=0
        self.model=Mock();self.model.plan_query.side_effect=ValueError('offline')
        self.model.knowledge_qa.return_value='离线资料[E1]'
        self.graph=Mock(return_value=found(fact(),fact('desc','离线资料说明。')))
        for p in [patch('api.views.DeepSeekAPI',return_value=self.model),patch('api.views.retrieve_context',self.graph),
                  patch('api.views.retrieve_phrase',return_value=found(fact()))]:p.start();self.addCleanup(p.stop)
    def qa(self,text,conversation=None,mid=None):
        self.n+=1
        return self.client.post('/api/ai/knowledge-qa/',{'query':text,'conversation_id':(conversation or self.c).id,
            'client_message_id':mid or str(self.n)},format='json')
    def settings(self,**kw):
        r=self.client.get('/api/memory/');self.assertEqual(r.status_code,200,r.data)
        r=self.client.patch('/api/memory/',{**kw,'version':r.data['version']},format='json')
        self.assertEqual(r.status_code,200,r.data);return r.data
    def proposal(self,text='记住，我有糖尿病。'):
        self.settings(health_collect_enabled=True)
        r=self.qa(text);self.assertEqual(r.status_code,200,r.data)
        self.assertIsNotNone(r.data.get('health_proposal_id'),r.data)
        return HealthMemoryProposal.objects.get(pk=r.data['health_proposal_id'])
    def confirm(self,p,**kw):
        return self.client.post(f'/api/memory/health/proposals/{p.id}/confirm/',{'profile_version':p.profile_version,**kw},format='json')
    def saved(self,text='记住，我有糖尿病。'):
        r=self.confirm(self.proposal(text));self.assertEqual(r.status_code,200,r.data)
        return HealthMemory.objects.get(pk=r.data['item']['id'])
    def action(self,row,action):
        return self.client.post(f'/api/memory/health/{row.id}/',{'action':action,'version':row.version},format='json')
    def new(self):return Conversation.objects.create(user=self.u)
    def test_default_off_command_truthful_and_no_candidate(self):
        r=self.qa('记住，我有糖尿病。');self.assertEqual(r.status_code,200,r.data)
        self.assertIn('尚未长期保存',r.data['answer']);self.assertEqual(HealthMemory.objects.count(),0)
        self.assertFalse(HealthMemoryProposal.objects.filter(status='pending').exists());self.graph.assert_not_called()
    def test_candidate_not_saved_or_used_before_confirmation(self):
        self.proposal();self.assertEqual(HealthMemory.objects.count(),0)
        r=self.qa('我的糖尿病有什么相关资料？',self.new())
        self.assertEqual(r.status_code,200,r.data);self.assertEqual(r.data['memory']['used_health'],[])
    def test_confirmed_read_in_new_conversation_and_fresh_orm(self):
        row=self.saved();self.assertIsNotNone(stored_report(HealthMemory.objects.get(pk=row.pk)))
        r=self.qa('我的糖尿病有什么相关资料？',self.new());self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['memory']['used_health'],[{'id':row.id,'version':1}])
        self.assertIn('已确认保存的用户自述',self.model.knowledge_qa.call_args.args[1])
    def test_general_and_family_questions_do_not_use_self_report(self):
        self.saved()
        for text in ['糖尿病是什么？','我想了解糖尿病。','我妈妈的糖尿病有什么相关资料？']:
            r=self.qa(text,self.new());self.assertEqual(r.status_code,200,r.data)
            self.assertEqual(r.data['memory']['used_health'],[],text)
    def test_ordinary_self_report_never_creates_proposal(self):
        self.settings(health_collect_enabled=True);self.qa('我有糖尿病。')
        self.assertEqual(HealthMemoryProposal.objects.count(),0);self.assertEqual(HealthMemory.objects.count(),0)
    def test_admitted_raw_states_and_original_span(self):
        for text,state in [('记住，我有糖尿病。','current'),('请记住，我上周感冒了。','past'),
                           ('记住，我的感冒已经好了。','resolved'),('记住，我没有糖尿病。','denied'),
                           ('记住，我可能有糖尿病。','uncertain')]:
            r=parse_command(text,'m',timezone.now().isoformat(),'user:1','c:1')
            self.assertIsNotNone(r,text);self.assertEqual(r['event_state'],state)
            self.assertEqual(text[r['source']['start']:r['source']['end']],r['source']['text'])
    def test_unsupported_inputs_are_not_saved(self):
        self.settings(health_collect_enabled=True)
        for text in ['记住，我妈妈有糖尿病。','记住，如果我有糖尿病。','记住，我有糖尿病吗？',
                     '记住，我对花粉过敏。','记住，我每天吃二片药。','记住，我有糖尿病，想了解高血压。',
                     '记住，我不是没有糖尿病。','记住，“我有糖尿病”。','记住，我有糖尿病，我妈妈有高血压。']:
            r=self.qa(text);self.assertEqual(r.status_code,200,r.data);self.assertIsNone(r.data.get('health_proposal_id'),text)
        self.assertEqual(HealthMemory.objects.count(),0);self.graph.assert_not_called()
    def test_explicit_preference_route_preserved(self):
        r=self.qa('记住，以后回答简短一些。');self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(HealthMemoryProposal.objects.count(),0)
    def test_same_name_requires_target_or_new_event(self):
        row=self.saved('记住，我上周感冒了。');p=self.proposal('记住，我昨天感冒了。')
        self.assertEqual(self.confirm(p).status_code,400)
        r=self.confirm(p,new_event=True);self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(HealthMemory.objects.count(),2)
        self.assertNotEqual(HealthMemory.objects.last().report['event_id'],row.report['event_id'])
    def test_explicit_target_update_uses_contract_and_keeps_event_identity(self):
        row=self.saved();old_source=copy.deepcopy(row.report['source'])
        p=self.proposal('记住，我没有糖尿病。')
        r=self.confirm(p,replace_id=row.id,target_version=row.version);self.assertEqual(r.status_code,200,r.data)
        row.refresh_from_db();self.assertEqual(row.version,2);self.assertEqual(row.report['event_state'],'denied')
        self.assertIsNotNone(stored_report(row));self.assertTrue(ChatMessage.objects.filter(pk=int(old_source['message_id'][3:])).exists())
    def test_conflicting_current_report_clarifies_before_tools_without_overwriting(self):
        row=self.saved();self.graph.reset_mock();self.model.reset_mock()
        r=self.qa('我没有糖尿病。',self.new());self.assertEqual(r.status_code,200,r.data)
        self.assertIn('尚未自动修改',r.data['answer']);self.graph.assert_not_called();self.model.knowledge_qa.assert_not_called()
        row.refresh_from_db();self.assertEqual(row.report['event_state'],'current')
    def test_usage_off_blocks_use_but_keeps_entries(self):
        self.saved();self.settings(enabled=False)
        r=self.qa('我的糖尿病有什么相关资料？',self.new());self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['memory']['used_health'],[]);self.assertEqual(HealthMemory.objects.count(),1)
    def test_collection_off_rejects_pending_confirmation(self):
        p=self.proposal();self.settings(health_collect_enabled=False)
        self.assertEqual(self.confirm(p).status_code,400);self.assertEqual(HealthMemory.objects.count(),0)
    def test_pause_and_replayed_confirmation_do_not_reactivate(self):
        p=self.proposal();r=self.confirm(p);row=HealthMemory.objects.get(pk=r.data['item']['id'])
        self.assertEqual(self.action(row,'pause').status_code,200)
        self.assertEqual(self.confirm(p).status_code,200)
        row.refresh_from_db();self.assertEqual(row.status,'paused')
        r=self.qa('我的糖尿病有什么资料？',self.new());self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['memory']['used_health'],[])
    def test_forget_clears_report_and_old_command_confirmation_cannot_resurrect(self):
        self.settings(health_collect_enabled=True);r=self.qa('记住，我有糖尿病。',mid='stable')
        p=HealthMemoryProposal.objects.get(pk=r.data['health_proposal_id']);self.confirm(p)
        row=HealthMemory.objects.get();before=self.c.messages.count()
        self.assertEqual(self.action(row,'forget').status_code,200);row.refresh_from_db();self.assertEqual(row.report,{})
        self.qa('记住，我有糖尿病。',mid='stable');self.confirm(p)
        row.refresh_from_db();self.assertEqual(row.status,'forgotten');self.assertEqual(self.c.messages.count(),before)
        r=self.qa('我的糖尿病有什么资料？',self.c);self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['memory']['used_health'],[])
    def test_fresh_request_after_forgetting_allowed_but_old_pending_invalid(self):
        row=self.saved();old=self.proposal('记住，我有糖尿病。')
        self.action(row,'forget');self.assertEqual(self.confirm(old).status_code,400)
        self.assertEqual(self.confirm(self.proposal()).status_code,200)
    def test_source_change_or_reset_rejects_candidate(self):
        p=self.proposal();ChatMessage.objects.filter(pk=p.source_message_id).update(content='我没有糖尿病。')
        self.assertEqual(self.confirm(p).status_code,400)
        p=self.proposal();self.client.post(f'/api/conversations/{self.c.id}/memory/',{'action':'reset'},format='json')
        self.assertEqual(self.confirm(p).status_code,400)
    def test_source_and_semantic_tamper_prevent_selection(self):
        row=self.saved();row.report['event_state']='resolved';row.save();self.assertIsNone(stored_report(row))
    def test_candidate_expiry_and_changed_profile_rejected(self):
        p=self.proposal();HealthMemoryProposal.objects.filter(pk=p.pk).update(created_at=timezone.now()-timedelta(minutes=11))
        self.assertEqual(self.confirm(p).status_code,400)
        p=self.proposal();self.settings(enabled=False);self.assertEqual(self.confirm(p).status_code,400)
    def test_account_isolation_for_read_confirm_manage_and_conversation(self):
        row=self.saved();p=self.proposal();other=User.objects.create_user(username='other-health')
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get('/api/memory/').data['health_items'],[])
        self.assertEqual(self.confirm(p).status_code,400);self.assertEqual(self.action(row,'forget').status_code,400)
        self.assertEqual(self.qa('记住，我有糖尿病。').status_code,404)
    def test_guest_cannot_claim_saved_or_call_confirm(self):
        self.client.force_authenticate(None)
        r=self.client.post('/api/ai/knowledge-qa/',{'query':'记住，我有糖尿病。'},format='json')
        self.assertEqual(r.status_code,200,r.data);self.assertIn('尚未长期保存',r.data['answer'])
        self.assertIn(self.client.post('/api/memory/health/proposals/1/confirm/',{},format='json').status_code,[401,403])
    def test_message_replay_different_content_rejected_and_off_receipt_stays_rejected(self):
        self.qa('记住，我有糖尿病。',mid='x');self.settings(health_collect_enabled=True)
        self.qa('记住，我有糖尿病。',mid='x')
        self.assertEqual(self.qa('记住，我有高血压。',mid='x').status_code,400)
        self.assertFalse(HealthMemoryProposal.objects.filter(status='pending').exists())
    def test_write_failure_rolls_back_and_does_not_claim_saved(self):
        p=self.proposal()
        with patch('api.controlled_health.HealthMemory.objects.create',side_effect=OperationalError('offline failure')):
            r=self.confirm(p)
        self.assertEqual(r.status_code,503,r.data);self.assertIn('没有确认',r.data['error'])
        self.assertEqual(HealthMemory.objects.count(),0);p.refresh_from_db();self.assertEqual(p.status,'pending')
    def test_old_control_sources_not_in_history_or_reused_after_forgetting(self):
        history=[{'role':'user','content':'记住，我有糖尿病。'},{'role':'assistant','content':'候选'},
                 {'role':'user','content':'高血压是什么？'}]
        self.assertEqual(strip_control_history(history),history[2:])
    def test_multiple_same_disease_no_silent_first_choice_or_stale_target(self):
        row=self.saved();p=self.proposal('记住，我没有糖尿病。')
        self.assertEqual(self.confirm(p,replace_id=row.id,target_version=99).status_code,400)
        self.assertEqual(self.confirm(p,replace_id=True,target_version=1).status_code,400)
    def test_confirmation_idempotency_and_no_duplicate_chat(self):
        p=self.proposal();self.confirm(p);self.confirm(p)
        self.assertEqual(HealthMemory.objects.count(),1);self.assertEqual(self.c.messages.count(),2)
    def test_health_change_during_answer_discards_stale_result(self):
        row=self.saved()
        def mutate(*a,**k):self.action(row,'pause');return '过时回答[E1]'
        self.model.knowledge_qa.side_effect=mutate
        r=self.qa('我的糖尿病有什么资料？',self.new())
        self.assertEqual(r.status_code,409,r.data);self.assertFalse(ChatMessage.objects.filter(content__contains='过时回答').exists())

    def test_multiple_events_ask_time_and_explicit_time_selects_only_match(self):
        first=self.saved('记住，我上周感冒了。')
        p=self.proposal('记住，我去年感冒了。');self.assertEqual(self.confirm(p,new_event=True).status_code,200)
        self.graph.reset_mock()
        r=self.qa('我的感冒有什么相关资料？',self.new());self.assertEqual(r.status_code,200,r.data)
        self.assertIn('哪次',r.data['answer']);self.graph.assert_not_called()
        r=self.qa('我上周的感冒有什么相关资料？',self.new());self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['memory']['used_health'],[{'id':first.id,'version':1}])

    def test_declined_candidate_cannot_be_confirmed_later(self):
        p=self.proposal();self.assertEqual(self.confirm(p,dismiss=True).status_code,200)
        self.assertEqual(self.confirm(p).status_code,400);self.assertEqual(HealthMemory.objects.count(),0)

    def test_mixed_preference_and_health_command_not_partially_saved(self):
        self.settings(health_collect_enabled=True)
        r=self.qa('记住，我有糖尿病，以后简短点。');self.assertEqual(r.status_code,200,r.data)
        self.assertIsNone(r.data.get('health_proposal_id'));self.graph.assert_not_called()

    def test_collection_disabled_does_not_stop_use_of_confirmed_records(self):
        row=self.saved();self.settings(health_collect_enabled=False)
        r=self.qa('我的糖尿病有什么资料？',self.new());self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['memory']['used_health'],[{'id':row.id,'version':1}])

    def test_candidate_write_failure_rolls_back_source_messages(self):
        self.settings(health_collect_enabled=True);before=self.c.messages.count()
        with patch('api.controlled_health.HealthMemoryProposal.objects.create',side_effect=OperationalError('offline')):
            r=self.qa('记住，我有糖尿病。')
        self.assertEqual(r.status_code,503,r.data);self.assertEqual(self.c.messages.count(),before)

    def test_state_budget_is_checked_before_tools_and_final_bytes_remain_guarded(self):
        # Force large independently verified rows at the read boundary; this
        # tests production placement of the guard, not the extraction grammar.
        rows=[{'id':1,'version':1,'report':{'value':'糖尿病','source':{'text':'字'*5000}}}]
        with patch('api.views.context_health',return_value=(rows,None)):
            r=self.qa('我的糖尿病有什么资料？',self.new())
        self.assertEqual(r.status_code,400,r.data);self.graph.assert_not_called()

    def test_health_save_does_not_rewrite_existing_finite_preferences(self):
        from .models import UserMemory
        self.qa('以后简短点。');before=list(UserMemory.objects.values())
        self.saved();self.assertEqual(list(UserMemory.objects.values()),before)
