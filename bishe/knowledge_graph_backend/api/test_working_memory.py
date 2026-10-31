"""Developer-visible composition tests with real ORM/source checks and model doubles."""
import copy
import json
import uuid
from unittest.mock import Mock,patch
from django.contrib.auth.models import User
from django.core import signing
from django.test import override_settings
from rest_framework.test import APITestCase
from .chat_runtime import ChatAgent,Checkpoints,RUNTIME_ROOT,source_catalog,SALT
from .planning_tools import SnapshotTools
from .planning_runtime import entity_id,fingerprint,BoundaryError
from .test_chat_runtime import Planner
from .models import Conversation,ChatMessage,MemorySettings,HealthMemory,HealthMemoryProposal,UserMemory
from .working_memory import receipt


@override_settings(DOCUMENT_RAG_ENABLED=False)
class WorkingMemoryTests(APITestCase):
    def setUp(self):
        self.u=User.objects.create_user(username='stageG-synthetic')
        self.c=Conversation.objects.create(user=self.u)
        self.client.force_authenticate(self.u)
        self.catalog=source_catalog()
        self.tools=Mock(wraps=SnapshotTools(self.catalog,[entity_id('糖尿病')]))
        self.checkpoints=Checkpoints(RUNTIME_ROOT/('stageG-test-'+uuid.uuid4().hex))
        self.model=Mock();self.model.knowledge_qa.return_value='合成回答[E1]'
        self.model.last_answer_usage={'prompt_tokens':20,'completion_tokens':5,'total_tokens':25}
        self.model.plan_query.side_effect=ValueError('offline')
        self.query='我的糖尿病的并发疾病中，选前2个，列出描述和预防资料。'
        for p in [patch('api.views.DeepSeekAPI',return_value=self.model),
                  patch('api.chat_runtime.ReadOnlyTools',return_value=self.tools),
                  patch('api.chat_runtime.ProductionPlanner',Planner),
                  patch('api.chat_runtime.Checkpoints',return_value=self.checkpoints)]:
            p.start();self.addCleanup(p.stop)
    def qa(self,q=None,**kw):
        return self.client.post('/api/ai/knowledge-qa/',{'query':q or self.query,'conversation_id':self.c.id,
            'client_message_id':uuid.uuid4().hex,**kw},format='json')
    def pause(self,steps=1,q=None):
        r=self.qa(q,pause_after_steps=steps);self.assertEqual(r.status_code,200,r.data)
        self.assertIn('resume_token',r.data['retrieval'],r.data);return r.data
    def resume(self,p,**kw):return self.qa('继续整理',resume_token=p['retrieval']['resume_token'],**kw)
    def saved(self):
        s,_=MemorySettings.objects.get_or_create(user=self.u)
        s.health_collect_enabled=True;s.save()
        r=self.qa('记住，我有糖尿病。');self.assertEqual(r.status_code,200,r.data)
        p=HealthMemoryProposal.objects.get(pk=r.data['health_proposal_id'])
        r=self.client.post(f'/api/memory/health/proposals/{p.id}/confirm/',{'profile_version':p.profile_version},format='json')
        self.assertEqual(r.status_code,200,r.data)
        self.c=Conversation.objects.create(user=self.u)
        return HealthMemory.objects.get(pk=r.data['item']['id'])
    def state_path(self,p):
        v=signing.loads(p['retrieval']['resume_token'],salt=SALT)
        return self.checkpoints.root/(v['key']+'.state.json')
    def assert_no_more_calls(self,p):
        reads=self.tools.read.call_count;answers=self.model.knowledge_qa.call_count
        r=self.resume(p);self.assertIn(r.status_code,(400,409),r.data)
        self.assertEqual(self.tools.read.call_count,reads);self.assertEqual(self.model.knowledge_qa.call_count,answers)
        return r
    def test_original_health_in_actual_resumed_answer(self):
        row=self.saved();p=self.pause();r=self.resume(p)
        self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['memory']['used_health'],[{'id':row.id,'version':1}])
        self.assertIn('已确认保存的用户自述',self.model.knowledge_qa.call_args.args[1])
        self.assertIn('我有糖尿病',self.model.knowledge_qa.call_args.args[1])
    def test_original_turn_preference_in_actual_resumed_answer(self):
        UserMemory.objects.create(user=self.u,kind='length',value='detailed')
        p=self.pause(q=self.query+'这次简短点。');r=self.resume(p)
        self.assertEqual(r.status_code,200,r.data)
        used=r.data['memory']['used_long_term']
        self.assertEqual([x['description'] for x in used],['简短回答，保留必要限制与出处'])
        self.assertEqual(UserMemory.objects.get(user=self.u,kind='length').value,'detailed')
        self.assertIn('简短回答，保留必要限制与出处',self.model.knowledge_qa.call_args.args[1])
    def test_unsigned_state_expiry_edit_cannot_pass(self):
        p=self.pause();path=self.state_path(p)
        data=json.loads(path.read_text(encoding='utf-8'));data['payload']['expires_at']+=100
        data['sha256']=fingerprint(data['payload']);path.write_text(json.dumps(data),encoding='utf-8')
        self.assert_no_more_calls(p)
    def test_pause_audit_has_selected_not_yet_applied_memory(self):
        row=self.saved();p=self.pause()
        a=p['retrieval']['task']['working_memory']
        self.assertEqual(a['selected_memory']['health'],[{'id':row.id,'version':1}])
        self.assertEqual(p['memory']['used_health'],[])
        self.assertEqual(a['counts']['cumulative'],{'reads':1,'decisions':0,'answers':0})
        self.assertEqual(len(a['pending']),4);self.assertTrue(a['evidence_ids'])
    def test_resume_counts_evidence_and_unknown_cost(self):
        p=self.pause();r=self.resume(p);self.assertEqual(r.status_code,200,r.data)
        a=r.data['retrieval']['task']['working_memory']
        self.assertTrue(a['resumed']);self.assertEqual(a['counts']['before_request']['reads'],1)
        self.assertEqual(a['counts']['this_request'],{'reads':2,'decisions':2,'answers':1})
        self.assertEqual(a['pending'],[]);self.assertIsNone(a['cost'])
        self.assertEqual([x['role'] for x in a['receipts']],['planner','planner','answer'])
        self.assertEqual(a['receipts'][-1]['usage']['total_tokens'],25)
        self.assertTrue(all(x['task_id']==a['task_id'] for x in a['receipts']))
        self.assertEqual(a['receipts'][0]['usage_status'],'unknown')
    def test_multiple_pauses_keep_prior_usage_and_one_relation(self):
        p=self.pause();r=self.resume(p,pause_after_steps=1);self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['retrieval']['task']['status'],'paused')
        r2=self.resume(r.data);self.assertEqual(r2.status_code,200,r2.data)
        a=r2.data['retrieval']['task']['working_memory']
        self.assertEqual(a['counts']['cumulative'],{'reads':3,'decisions':2,'answers':1})
        self.assertEqual(a['counts']['before_request']['decisions'],1)
        self.assertEqual(len(a['receipts']),3)
        self.assertEqual(len([c for c in self.tools.read.call_args_list if c.args[0]['kind']=='read_relations']),1)
    def test_profile_change_stops_before_tools(self):
        p=self.pause();s,_=MemorySettings.objects.get_or_create(user=self.u);s.version+=1;s.save()
        self.assert_no_more_calls(p)
    def test_reset_stops_before_tools(self):
        p=self.pause();self.client.post(f'/api/conversations/{self.c.id}/memory/',{'action':'reset'},format='json')
        self.assert_no_more_calls(p)
    def test_forgotten_selected_memory_stops_before_tools(self):
        row=self.saved();p=self.pause()
        self.client.post(f'/api/memory/health/{row.id}/',{'action':'forget','version':row.version},format='json')
        self.assert_no_more_calls(p);row.refresh_from_db();self.assertEqual(row.report,{})
    def test_archived_selected_memory_stops_before_tools(self):
        row=self.saved();p=self.pause()
        self.client.post(f'/api/memory/health/{row.id}/',{'action':'archive','version':row.version},format='json')
        self.assert_no_more_calls(p)
    def test_source_changed_without_profile_bump_stops(self):
        row=self.saved();p=self.pause();sid=int(row.report['source']['message_id'][3:])
        ChatMessage.objects.filter(pk=sid).update(content='来源改动')
        self.assert_no_more_calls(p)
    def test_preference_row_changed_without_profile_bump_stops(self):
        row=UserMemory.objects.create(user=self.u,kind='style',value='plain')
        p=self.pause();UserMemory.objects.filter(pk=row.pk).update(value='technical')
        self.assert_no_more_calls(p)
    def test_another_account_cannot_read_continuation(self):
        p=self.pause();other=User.objects.create_user(username='stageG-other');self.client.force_authenticate(other)
        r=self.resume(p);self.assertEqual(r.status_code,404)
        self.client.force_authenticate(self.u);self.assertEqual(self.resume(p).status_code,200)
    def test_another_conversation_cannot_claim_original(self):
        p=self.pause();original=self.c;self.c=Conversation.objects.create(user=self.u);self.assert_no_more_calls(p)
        self.c=original;self.assertEqual(self.resume(p).status_code,200)
    def test_consumed_continuation_no_extra_calls(self):
        p=self.pause();self.assertEqual(self.resume(p).status_code,200);self.assert_no_more_calls(p)
    def test_wrong_resume_query_rejected_without_claim(self):
        p=self.pause();r=self.qa('换个问题',resume_token=p['retrieval']['resume_token']);self.assertEqual(r.status_code,400)
        self.assertEqual(self.resume(p).status_code,200)
    def test_intervening_user_requirement_stops_old_task(self):
        p=self.pause();self.qa('接下来都不用表格。');self.assert_no_more_calls(p)
    def test_changed_dialogue_original_stops_old_task(self):
        m=ChatMessage.objects.create(conversation=self.c,role='user',content='之前在整理资料。')
        p=self.pause();ChatMessage.objects.filter(pk=m.pk).update(content='原话已经修改。');self.assert_no_more_calls(p)
    def test_actual_expired_wall_time_stops_without_renewal(self):
        p=self.pause()
        with patch('api.chat_runtime.signing.loads',side_effect=signing.SignatureExpired()):self.assert_no_more_calls(p)
    def test_frozen_dialogue_matches_actual_packet_and_cache(self):
        ChatMessage.objects.create(conversation=self.c,role='user',content='我昨天空腹血糖5.6 mmol/L，糖尿病资料如何解释？')
        p=self.pause(q=self.query+'结合空腹血糖数值资料。');r=self.resume(p);self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['memory']['dialogue'],p['memory']['dialogue'])
        self.c.refresh_from_db();self.assertEqual(self.c.context_notes['coverage_ids'],r.data['memory']['dialogue']['coverage_ids'])
        self.assertIn('5.6 mmol/L',self.model.knowledge_qa.call_args.args[1])
    def test_answer_failure_is_one_attempt_and_no_fake_usage(self):
        p=self.pause();self.model.knowledge_qa.return_value=None;self.model.last_answer_usage=None
        r=self.resume(p);self.assertEqual(r.status_code,503,r.data)
        a=r.data['retrieval']['task']['working_memory'];self.assertEqual(a['counts']['cumulative']['answers'],1)
        self.assertEqual(a['receipts'][-1]['usage_status'],'unknown');self.assert_no_more_calls(p)
    def test_general_question_never_attaches_saved_self(self):
        self.saved();p=self.pause(q=self.query.replace('我的',''));r=self.resume(p)
        self.assertEqual(r.status_code,200,r.data);self.assertEqual(r.data['memory']['used_health'],[])
        self.assertNotIn('已确认保存的用户自述',self.model.knowledge_qa.call_args.args[1])
    def test_explicit_saved_reference_restores_original_selection_question(self):
        row=self.saved()
        p=self.pause(q='我之前记住的那个病的并发疾病中，选前2个，列出描述和预防资料。')
        r=self.resume(p);self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['memory']['used_health'],[{'id':row.id,'version':1}])
    def test_continue_does_not_promote_unrelated_oversized_old_original(self):
        ChatMessage.objects.create(conversation=self.c,role='user',content='我妈妈昨天空腹血糖5.6 mmol/L，'+'背景说明'*450+'，怎么记录？')
        p=self.pause();r=self.resume(p);self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['retrieval']['task']['status'],'complete')
        self.assertNotIn('5.6 mmol/L',self.model.knowledge_qa.call_args.args[1])
    def test_local_clarification_and_recall_still_zero_calls(self):
        self.saved();reads=self.tools.read.call_count
        r=self.qa('我没有糖尿病。');self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['retrieval']['status'],'ambiguous')
        self.qa('你记得我保存过哪些情况吗？')
        self.assertEqual(self.tools.read.call_count,reads);self.model.knowledge_qa.assert_not_called()
    def test_malformed_usage_never_zero_cost(self):
        for bad in [None,{},{'prompt_tokens':True,'completion_tokens':0,'total_tokens':1},
                    {'prompt_tokens':2,'completion_tokens':1,'total_tokens':2}]:
            r=receipt('t',1,'answer',{'usage':bad});self.assertIsNone(r['usage']);self.assertIsNone(r['cost'])
