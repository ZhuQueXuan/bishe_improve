"""F: actual ORM/API lifecycle, source exclusion and stale worker checks."""
import copy
from datetime import timedelta
from unittest.mock import Mock,patch
from django.contrib.auth.models import User
from django.db import OperationalError
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from .models import (Conversation,ChatMessage,MemorySettings,MemoryInputEvent,UserMemory,
                     HealthMemory,HealthMemoryProposal,MemorySourceExclusion,MemoryMaintenanceTask)
from .memory_lifecycle import create_task,claim_task,build_plan,commit_plan,run_task
from .user_memory import conversation_history
from .test_agent_loop import fact,found

@override_settings(DOCUMENT_RAG_ENABLED=False)
class MemoryLifecycleTests(APITestCase):
    def setUp(self):
        self.u=User.objects.create_user(username='stageF-synthetic');self.c=Conversation.objects.create(user=self.u)
        self.client.force_authenticate(self.u);self.n=0
        self.s=MemorySettings.objects.create(user=self.u,health_collect_enabled=True)
        self.model=Mock();self.model.plan_query.side_effect=ValueError('offline');self.model.knowledge_qa.return_value='离线说明[E1]'
        self.graph=Mock(return_value=found(fact(),fact('desc','离线资料说明。')))
        for p in [patch('api.views.DeepSeekAPI',return_value=self.model),patch('api.views.retrieve_context',self.graph),
                  patch('api.views.retrieve_phrase',return_value=found(fact()))]:p.start();self.addCleanup(p.stop)
    def qa(self,q,mid=None,c=None):
        self.n+=1
        return self.client.post('/api/ai/knowledge-qa/',{'query':q,'conversation_id':(c or self.c).id,'client_message_id':mid or str(self.n)},format='json')
    def proposal(self,q='记住，我有糖尿病。',mid=None):
        r=self.qa(q,mid);self.assertEqual(r.status_code,200,r.data)
        return HealthMemoryProposal.objects.get(pk=r.data['health_proposal_id'])
    def confirm(self,p,**extra):
        return self.client.post(f'/api/memory/health/proposals/{p.id}/confirm/',{'profile_version':p.profile_version,**extra},format='json')
    def saved(self,q='记住，我有糖尿病。'):
        p=self.proposal(q);r=self.confirm(p);self.assertEqual(r.status_code,200,r.data)
        return HealthMemory.objects.get(pk=r.data['item']['id'])
    def action(self,row,action):
        return self.client.post(f'/api/memory/health/{row.id}/',{'action':action,'version':row.version},format='json')
    def task(self,key=None):
        self.s.refresh_from_db();return create_task(self.u,key or f'task-{self.n}',self.s.version)
    def test_health_forget_excludes_all_confirmed_event_sources_and_keeps_chat(self):
        row=self.saved('记住，我上周感冒了。');p=self.proposal('记住，我的感冒已经好了。')
        r=self.confirm(p,replace_id=row.id,target_version=1);self.assertEqual(r.status_code,200,r.data);row.refresh_from_db()
        count=self.c.messages.count();self.assertEqual(self.action(row,'forget').status_code,200)
        self.assertEqual(MemorySourceExclusion.objects.filter(user=self.u).count(),2)
        row.refresh_from_db();self.assertEqual(row.report,{});self.assertEqual(self.c.messages.count(),count)
        self.assertEqual(conversation_history(self.c),[])
    def test_preference_forget_excludes_origin_and_assistant_but_not_next_turn(self):
        self.qa('以后简短点。',mid='origin');row=UserMemory.objects.get()
        self.assertIsNotNone(MemoryInputEvent.objects.get().source_message_id)
        self.qa('高血压是什么？')
        r=self.client.post(f'/api/memory/{row.id}/forget/',{'version':row.version},format='json');self.assertEqual(r.status_code,200,r.data)
        text=str(conversation_history(self.c));self.assertNotIn('以后简短',text);self.assertIn('高血压',text)
        self.assertEqual(MemorySourceExclusion.objects.count(),1)
    def test_language_forget_uses_same_persistent_source_exclusion(self):
        self.qa('以后简短点。',mid='origin');r=self.qa('忘记我的篇幅偏好。')
        self.assertEqual(r.status_code,200,r.data);self.assertEqual(MemorySourceExclusion.objects.count(),1)
        self.qa('以后简短点。',mid='origin');self.assertEqual(UserMemory.objects.get().status,'forgotten')
    def test_legacy_preference_without_receipt_link_literal_source_excluded(self):
        source=ChatMessage.objects.create(conversation=self.c,role='user',content='我正在学护理。')
        row=UserMemory.objects.create(user=self.u,kind='background',value='nursing',source='conversation_explicit',
                                      source_text='我正在学护理',source_message_ref=f'conversation:{self.c.id}/legacy')
        r=self.client.post(f'/api/memory/{row.id}/forget/',{'version':1},format='json');self.assertEqual(r.status_code,200,r.data)
        self.assertTrue(MemorySourceExclusion.objects.filter(message_id=source.id).exists())
    def test_archive_is_reversible_and_does_not_mark_recovered(self):
        row=self.saved();original=copy.deepcopy(row.report)
        self.assertEqual(self.action(row,'archive').status_code,200);row.refresh_from_db()
        self.assertEqual(row.status,'archived');self.assertEqual(row.report,original)
        r=self.qa('你记得我保存过哪些情况吗？');self.assertEqual(r.data['memory']['used_health'],[])
        self.assertEqual(self.action(row,'activate').status_code,200);row.refresh_from_db();self.assertEqual(row.status,'active')
        self.assertEqual(MemorySourceExclusion.objects.count(),0)
    def test_archived_target_not_silently_updated_by_new_candidate(self):
        row=self.saved();self.action(row,'archive');p=self.proposal('记住，我没有糖尿病。')
        self.assertEqual(self.confirm(p,replace_id=row.id,target_version=2).status_code,400)
    def test_invalid_source_marked_and_cannot_activate_without_verification(self):
        row=self.saved();mid=int(row.report['source']['message_id'][3:])
        ChatMessage.objects.filter(pk=mid).update(content='已变更的原话')
        t=run_task(self.u,self.task().id);self.assertEqual(t.status,'completed')
        row.refresh_from_db();self.assertEqual(row.status,'invalid');self.assertEqual(self.action(row,'activate').status_code,400)
    def test_expired_or_old_version_candidate_becomes_expired(self):
        p=self.proposal();HealthMemoryProposal.objects.filter(pk=p.id).update(created_at=timezone.now()-timedelta(minutes=11))
        t=run_task(self.u,self.task().id);self.assertEqual(t.status,'completed');p.refresh_from_db();self.assertEqual(p.status,'expired')
        self.assertEqual(self.confirm(p).status_code,400)
    def test_reset_invalidates_old_proposal(self):
        p=self.proposal();self.client.post(f'/api/conversations/{self.c.id}/memory/',{'action':'reset'},format='json')
        run_task(self.u,self.task().id);p.refresh_from_db();self.assertEqual(p.status,'expired')
    def test_only_identical_source_and_event_duplicates_archived(self):
        row=self.saved();duplicate=HealthMemory.objects.create(user=self.u,report=copy.deepcopy(row.report))
        p=self.proposal();self.assertEqual(self.confirm(p,new_event=True).status_code,200)
        t=run_task(self.u,self.task().id);self.assertEqual(t.status,'completed')
        duplicate.refresh_from_db();row.refresh_from_db();self.assertEqual(duplicate.status,'archived');self.assertEqual(row.status,'active')
        self.assertEqual(HealthMemory.objects.filter(status='active').count(),2)
    def test_unaccessed_old_health_not_aged_into_recovery_or_forgetting(self):
        row=self.saved('记住，我去年感冒了。');HealthMemory.objects.filter(pk=row.id).update(updated_at=timezone.now()-timedelta(days=3650))
        original=copy.deepcopy(row.report);run_task(self.u,self.task().id);row.refresh_from_db()
        self.assertEqual(row.status,'active');self.assertEqual(row.report,original)
    def test_same_request_is_one_task_and_replay_no_version_overwrite(self):
        t=self.task('stable');result=run_task(self.u,t.id);self.assertEqual(result.status,'completed')
        again=create_task(self.u,'stable',t.profile_version);self.assertEqual(again.id,t.id)
        self.assertEqual(run_task(self.u,t.id).result,result.result);self.assertEqual(MemoryMaintenanceTask.objects.count(),1)
    def test_task_late_commit_after_forget_is_stale_not_resurrection(self):
        row=self.saved();t,token=claim_task(self.u,self.task().id);plan=build_plan(self.u)
        self.action(row,'forget');result=commit_plan(self.u,t.id,token,plan)
        self.assertEqual(result.status,'stale');row.refresh_from_db();self.assertEqual(row.report,{})
    def test_source_change_after_plan_prevents_old_commit(self):
        row=self.saved();t,token=claim_task(self.u,self.task().id);plan=build_plan(self.u)
        ChatMessage.objects.filter(pk=int(row.report['source']['message_id'][3:])).update(content='已变更的原话')
        result=commit_plan(self.u,t.id,token,plan);self.assertEqual(result.status,'stale')
    def test_plan_mutation_not_trusted_by_commit(self):
        self.saved();t,token=claim_task(self.u,self.task().id);plan=build_plan(self.u)
        plan['actions'].append({'kind':'invalid_health','id':999,'version':1})
        self.assertEqual(commit_plan(self.u,t.id,token,plan).status,'stale')
    def test_lease_expiry_reclaim_rejects_old_token(self):
        self.saved();t,oldtoken=claim_task(self.u,self.task().id)
        MemoryMaintenanceTask.objects.filter(pk=t.id).update(lease_until=timezone.now()-timedelta(seconds=1))
        new,newtoken=claim_task(self.u,t.id);self.assertNotEqual(oldtoken,newtoken)
        self.assertEqual(commit_plan(self.u,t.id,oldtoken,build_plan(self.u)).status,'running')
        self.assertEqual(commit_plan(self.u,t.id,newtoken,build_plan(self.u)).status,'completed')
    def test_failure_rolls_back_actions_and_attempts_bounded(self):
        row=self.saved();ChatMessage.objects.filter(pk=int(row.report['source']['message_id'][3:])).update(content='changed')
        t=self.task()
        with patch('api.memory_lifecycle.HealthMemory.save',side_effect=OperationalError('offline failure')):
            result=run_task(self.u,t.id);self.assertEqual(result.status,'failed')
            row.refresh_from_db();self.assertEqual(row.status,'active')
            result=run_task(self.u,t.id);self.assertEqual(result.attempts,2)
        self.assertEqual(run_task(self.u,t.id).status,'exhausted')
    def test_cache_source_change_and_exclusion_cleared_not_chat(self):
        row=self.saved();self.c.context_notes={'revision':f'0:{self.s.version}','recent':[{'source_message_id':row.report['source']['message_id'],
            'source':{'sha256':'incorrect'}}],'summary':{'notes':[]}};self.c.save()
        count=self.c.messages.count();run_task(self.u,self.task().id);self.c.refresh_from_db()
        self.assertEqual(self.c.context_notes,{});self.assertEqual(self.c.messages.count(),count)
    def test_malformed_cache_does_not_raise_or_look_success_without_cleanup(self):
        self.c.context_notes={'revision':'0:0','recent':'broken','summary':{'notes':False}};self.c.save()
        result=run_task(self.u,self.task().id);self.assertEqual(result.status,'completed')
        self.c.refresh_from_db();self.assertEqual(self.c.context_notes,{})
    def test_account_isolation_for_tasks_and_exclusions(self):
        row=self.saved();self.action(row,'forget');task=self.task()
        other=User.objects.create_user(username='stageF-other');MemorySettings.objects.create(user=other)
        with self.assertRaises(Exception):claim_task(other,task.id)
        self.assertEqual(MemorySourceExclusion.objects.filter(user=other).count(),0)
        self.client.force_authenticate(other);self.assertEqual(self.client.get('/api/memory/').data['maintenance_tasks'],[])
    def test_guest_cannot_request_maintenance(self):
        self.client.force_authenticate(None);r=self.client.post('/api/memory/maintenance/',{'request_key':'guest','version':0},format='json')
        self.assertIn(r.status_code,[401,403]);self.assertEqual(MemoryMaintenanceTask.objects.count(),0)
    def test_fresh_new_request_after_forget_can_be_confirmed(self):
        row=self.saved();self.action(row,'forget');new=self.saved()
        self.assertEqual(new.status,'active');self.assertNotEqual(new.id,row.id)
        self.assertEqual(MemorySourceExclusion.objects.count(),1)
    def test_excluded_tail_ids_do_not_appear_as_concurrent_messages(self):
        row=self.saved();self.action(row,'forget')
        result=self.qa('高血压是什么？');self.assertEqual(result.status_code,200,result.data)
    def test_duplicate_exclusion_insert_is_idempotent(self):
        from .memory_lifecycle import exclude_health_sources
        row=self.saved();exclude_health_sources(row);exclude_health_sources(row)
        self.assertEqual(MemorySourceExclusion.objects.count(),1)
    def test_worker_command_only_processes_selected_user_existing_jobs(self):
        from django.core.management import call_command
        self.task();other=User.objects.create_user(username='worker-other');MemorySettings.objects.create(user=other)
        other_task=create_task(other,'other',0)
        call_command('maintain_memory',user_id=self.u.id,verbosity=0)
        self.assertEqual(MemoryMaintenanceTask.objects.get(user=self.u).status,'completed')
        other_task.refresh_from_db();self.assertEqual(other_task.status,'queued')
    def test_bound_exceeded_returns_no_partial_changes(self):
        for i in range(51):Conversation.objects.create(user=self.u,context_notes={'unverified':i})
        task=run_task(self.u,self.task().id)
        self.assertTrue(task.result['requires_narrowing']);self.assertEqual(task.result['changed'],0)
    def test_task_results_contain_no_health_text(self):
        self.saved();t=run_task(self.u,self.task().id)
        self.assertNotIn('糖尿病',str(t.result));self.assertNotIn('记住',str(t.result))
    def test_maintenance_api_returns_actual_result_and_idempotent_task(self):
        row=self.saved();self.s.refresh_from_db();data={'request_key':'api-stable','version':self.s.version}
        r=self.client.post('/api/memory/maintenance/',data,format='json');self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['status'],'completed');self.assertEqual(r.data['result']['changed'],0)
        again=self.client.post('/api/memory/maintenance/',data,format='json');self.assertEqual(again.data['id'],r.data['id'])
        row.refresh_from_db();self.assertEqual(row.status,'active')
    def test_maintenance_failure_api_reports_uncompleted_and_does_not_claim_success(self):
        self.saved();self.s.refresh_from_db()
        with patch('api.memory_lifecycle.build_plan',side_effect=ValueError('offline failure')):
            r=self.client.post('/api/memory/maintenance/',{'request_key':'failure','version':self.s.version},format='json')
        self.assertEqual(r.status_code,503,r.data);self.assertEqual(r.data['status'],'failed')
    def test_live_lease_is_not_claimed_by_duplicate_worker(self):
        t,token=claim_task(self.u,self.task().id);again,another=claim_task(self.u,t.id)
        self.assertIsNone(another);self.assertEqual(again.attempts,1)
    def test_forgot_source_cannot_be_accepted_by_old_pending_candidate(self):
        row=self.saved();pending=self.proposal();self.action(row,'forget')
        self.assertEqual(self.confirm(pending).status_code,400)
        run_task(self.u,self.task().id);pending.refresh_from_db();self.assertEqual(pending.status,'expired')
    def test_valid_current_cache_kept_when_no_source_or_version_changed(self):
        self.qa('糖尿病是什么？');self.c.refresh_from_db();before=copy.deepcopy(self.c.context_notes)
        self.assertTrue(before)
        task=run_task(self.u,self.task().id);self.assertEqual(task.status,'completed')
        self.c.refresh_from_db();self.assertEqual(self.c.context_notes,before)
    def test_forgetting_rolls_back_if_source_exclusion_write_fails(self):
        row=self.saved()
        with patch('api.memory_lifecycle.MemorySourceExclusion.objects.get_or_create',side_effect=OperationalError('offline')):
            self.client.raise_request_exception=False
            r=self.action(row,'forget')
        self.assertGreaterEqual(r.status_code,500);row.refresh_from_db();self.assertEqual(row.status,'active')
        self.assertTrue(row.report);self.assertEqual(MemorySourceExclusion.objects.count(),0)
