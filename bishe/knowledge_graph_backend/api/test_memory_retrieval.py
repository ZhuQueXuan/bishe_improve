"""Developer-visible finite retrieval matrix, temporal and production API checks."""
import copy
import json
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import Mock,patch
from django.contrib.auth.models import User
from django.test import SimpleTestCase,override_settings
from rest_framework.test import APITestCase
from .models import Conversation,ChatMessage,MemorySettings,HealthMemory
from .controlled_health import parse_command,context_health,strip_control_history
from .memory_retrieval import personal_plan,interval
from .session_health import SessionHealth
from .chat_runtime import source_catalog,intent_names
from .test_agent_loop import fact,found

FIXTURE=Path(__file__).resolve().parents[2]/'data/memory_system_v1/stageD_acceptance.json'
NOW='2026-10-26T12:00:00+08:00'

def names(text,multiple=False):return intent_names(text,[e['name'] for e in source_catalog().entities.values()],multiple=multiple)
def health(query,history=()):
    h=SessionHealth('test',names)
    for i,text in enumerate(history):h.ingest(text,f'old:{i}',NOW)
    h.ingest(query,'current',NOW)
    return h
def rows():
    f=json.loads(FIXTURE.read_text(encoding='utf-8'))
    return [{'id':i,'version':1,'key':key,'report':parse_command(text,f'm{i}',NOW,'user:1','source')}
            for i,(key,text) in enumerate(f['records'],1)]
def plan(query,items=None,h=None,now=NOW,**kw):
    return personal_plan(query,items if items is not None else rows(),h or health(query),names(query,multiple=True),now,**kw)

class MemorySelectionTests(SimpleTestCase):
    def test_preregistered_18_raw_queries(self):
        f=json.loads(FIXTURE.read_text(encoding='utf-8'))
        for case in f['cases']:
            with self.subTest(case=case['id']):
                p=plan(case['query'])
                self.assertEqual(sorted(m['key'] for m in p['rows']),sorted(case['selected']),p)
                self.assertEqual(bool(p['clarification']),case['clarify'],p)
    def test_relative_periods_use_source_year_not_query_year(self):
        item=rows()[2]
        p=plan('我去年的感冒有什么资料？',[item],now='2027-10-26T12:00:00+08:00')
        self.assertEqual(p['rows'],[])
        p=plan('我2025年的感冒有什么资料？',[item],now='2027-10-26T12:00:00+08:00')
        self.assertEqual(len(p['rows']),1)
    def test_new_week_not_same_old_raw_last_week(self):
        p=plan('我上周的感冒有什么资料？',[rows()[1]],now='2026-11-02T12:00:00+08:00')
        self.assertEqual(p['rows'],[])
    def test_calendar_boundary_and_timezone(self):
        self.assertEqual(interval('去年','2025-12-31T17:00:00Z'),interval('2025年',NOW))
        week=interval('上周',NOW);self.assertEqual(week[0].isoformat(),'2026-10-19');self.assertEqual(week[1].isoformat(),'2026-10-26')
    def test_specific_month_cannot_be_inferred_from_year_only(self):
        p=plan('我去年12月的感冒有什么资料？',[rows()[2]])
        self.assertEqual(p['rows'],[]);self.assertEqual(p['audit']['status'],'time_precision_unclear')
    def test_invalid_dates_and_multiple_periods_clarify(self):
        for query in ['我2025年13月的感冒有什么资料？','我上周和去年的感冒有什么资料？']:
            p=plan(query);self.assertTrue(p['clarification']);self.assertEqual(p['rows'],[])
    def test_one_saved_disease_can_resolve_explicit_saved_reference(self):
        p=plan('我之前记住的那个病的预防资料是什么？',[rows()[0]])
        self.assertEqual(len(p['rows']),1);self.assertEqual(p['audit']['reference'],'explicit_saved_reference_unique_disease')
    def test_personal_pronoun_uses_current_self_topic_but_not_family(self):
        q='它的预防资料是什么？';h=health(q,['我的糖尿病有哪些资料？']);h.discussed_names=['糖尿病']
        self.assertEqual(len(plan(q,h=h)['rows']),1)
        h=health(q,['我妈妈的糖尿病有哪些资料？']);h.discussed_names=['糖尿病']
        self.assertEqual(plan(q,h=h)['rows'],[])
    def test_unknown_person_or_generic_query_never_selects_all(self):
        for q in ['我的情况有什么资料？','如果我有糖尿病怎么办？','你记得我朋友的情况吗？']:
            self.assertEqual(plan(q)['rows'],[],q)
    def test_bounded_selection_does_not_silently_truncate(self):
        data=rows()+[{**rows()[0],'id':6,'key':'another'}]
        p=plan('你记得我保存过哪些情况吗？',data)
        self.assertEqual(p['rows'],[]);self.assertEqual(p['audit']['status'],'too_many_relevant')
    def test_no_arbitrary_recency_or_importance_discards_historical_event(self):
        p=plan('我2025年的感冒有什么资料？',list(reversed(rows())))
        self.assertEqual([r['key'] for r in p['rows']],['cold_year'])
    def test_monday_last_week_contains_yesterday_both_episodes_require_clarification(self):
        extra={'id':6,'version':1,'key':'yesterday','report':parse_command('记住，我昨天感冒了。','m6',NOW,'user:1','s')}
        p=plan('我上周的感冒有什么资料？',[rows()[1],extra])
        self.assertEqual(p['rows'],[]);self.assertEqual(p['audit']['status'],'episode_unclear')
    def test_current_session_does_not_displace_explicit_past_question(self):
        q='我2025年的感冒有什么资料？';p=plan(q,h=health(q,['我今天感冒了。']))
        self.assertEqual([r['key'] for r in p['rows']],['cold_year'])
    def test_current_report_conflict_and_separate_historical_statement(self):
        p=plan('我没有糖尿病。');self.assertEqual(p['audit']['status'],'conflict')
        p=plan('我上周没有糖尿病。');self.assertNotEqual(p['audit']['status'],'conflict')
    def test_excluded_audit_never_copies_other_record_content(self):
        p=plan('我的糖尿病有什么资料？');text=json.dumps(p['audit'],ensure_ascii=False)
        self.assertNotIn('感冒',text);self.assertNotIn('原话',text)
        self.assertEqual(p['audit']['selected'][0]['id'],1)
    def test_recall_result_never_asserts_current_diagnosis(self):
        p=plan('你记得我保存过哪些情况吗？')
        self.assertIn('不能据此判断你今天是否患病',p['local_answer'])
        self.assertIn('当时否认',p['local_answer']);self.assertIn('用户报告已结束',p['local_answer'])
    def test_recall_user_and_assistant_pair_removed_from_later_history(self):
        history=[{'role':'user','content':'你记得我保存过哪些情况吗？'},
                 {'role':'assistant','content':'旧的自述回忆'}, {'role':'user','content':'糖尿病是什么？'}]
        self.assertEqual(strip_control_history(history),history[2:])
    def test_off_and_guest_do_not_expose_rows_or_count(self):
        for kw in [{'enabled':False},{'authenticated':False}]:
            p=plan('你记得我保存过哪些情况吗？',**kw)
            self.assertEqual(p['rows'],[]);self.assertEqual(p['audit']['selected'],[])
    def test_unsupported_recall_topic_does_not_dump_health_profile(self):
        p=plan('你记得我喜欢什么电影吗？')
        self.assertEqual(p['rows'],[]);self.assertEqual(p['audit']['status'],'recall_type_unclear')


@override_settings(DOCUMENT_RAG_ENABLED=False)
class PersonalRetrievalAPITests(APITestCase):
    def setUp(self):
        self.u=User.objects.create_user(username='stageD-synthetic');self.c=Conversation.objects.create(user=self.u)
        self.client.force_authenticate(self.u);self.n=0
        self.s=MemorySettings.objects.create(user=self.u,health_collect_enabled=True)
        self.model=Mock();self.model.plan_query.side_effect=ValueError('offline')
        self.model.knowledge_qa.return_value='离线资料[E1]'
        self.graph=Mock(return_value=found(fact(),fact('desc','离线资料。')))
        for p in [patch('api.views.DeepSeekAPI',return_value=self.model),patch('api.views.retrieve_context',self.graph),
                  patch('api.views.retrieve_phrase',return_value=found(fact()))]:p.start();self.addCleanup(p.stop)
    def qa(self,q,c=None):
        self.n+=1
        return self.client.post('/api/ai/knowledge-qa/',{'query':q,'conversation_id':(c or self.c).id,'client_message_id':str(self.n)},format='json')
    def save(self,text='记住，我有糖尿病。'):
        r=self.qa(text);self.assertEqual(r.status_code,200,r.data)
        p=self.client.get('/api/memory/').data['health_proposals'][0]
        payload={'profile_version':p['profile_version']}
        if p['matching_ids']:payload['new_event']=True
        r=self.client.post(f"/api/memory/health/proposals/{p['id']}/confirm/",payload,format='json')
        self.assertEqual(r.status_code,200,r.data);return HealthMemory.objects.get(pk=r.data['item']['id'])
    def fresh(self):return Conversation.objects.create(user=self.u)
    def test_local_recall_returns_provenance_zero_tools(self):
        row=self.save();r=self.qa('你记得我保存过哪些情况吗？',self.fresh())
        self.assertEqual(r.status_code,200,r.data);self.assertEqual(r.data['retrieval']['status'],'memory_recall')
        self.assertIn('糖尿病',r.data['answer']);self.assertFalse(r.data['has_context']);self.assertEqual(r.data['sources'],[])
        self.assertEqual(r.data['memory']['used_health'],[{'id':row.id,'version':1}])
        self.graph.assert_not_called();self.model.plan_query.assert_not_called();self.model.knowledge_qa.assert_not_called()
    def test_relevant_background_reaches_answer_and_audit_excludes_other_content(self):
        row=self.save();self.save('记住，我上周感冒了。')
        r=self.qa('我的糖尿病有什么资料？',self.fresh());self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['memory']['selection']['selected'][0]['id'],row.id)
        self.assertIn('已确认保存的用户自述',self.model.knowledge_qa.call_args.args[1])
        self.assertNotIn('health:感冒',self.model.knowledge_qa.call_args.args[1])
    def test_paused_forgotten_pending_and_other_account_excluded(self):
        row=self.save();HealthMemory.objects.filter(pk=row.id).update(status='paused')
        self.qa('记住，我上周感冒了。')
        r=self.qa('你记得我保存过哪些情况吗？',self.fresh());self.assertEqual(r.status_code,200,r.data)
        self.assertEqual(r.data['memory']['used_health'],[]);self.assertNotIn('糖尿病',r.data['answer']);self.assertNotIn('感冒',r.data['answer'])
        other=User.objects.create_user(username='stageD-other');self.client.force_authenticate(other)
        r=self.client.post('/api/ai/knowledge-qa/',{'query':'你记得我保存过哪些情况吗？'},format='json')
        self.assertEqual(r.status_code,200,r.data);self.assertEqual(r.data['memory']['used_health'],[])
    def test_source_tamper_not_recalled(self):
        row=self.save();row.report['value']='高血压';row.save()
        r=self.qa('你记得我保存过哪些情况吗？',self.fresh())
        self.assertEqual(r.status_code,200,r.data);self.assertNotIn('高血压',r.data['answer'])
        self.assertEqual(r.data['memory']['selection']['excluded'][0]['reason'],'source_unverifiable')
    def test_forget_removes_old_recalled_answer_from_model_history_and_cache(self):
        row=self.save();c=self.fresh();self.qa('你记得我保存过哪些情况吗？',c)
        self.client.post(f'/api/memory/health/{row.id}/',{'action':'forget','version':row.version},format='json')
        r=self.qa('高血压是什么？',c);self.assertEqual(r.status_code,200,r.data)
        self.assertNotIn('health:糖尿病',self.model.knowledge_qa.call_args.args[1]);self.assertNotIn('当时自述存在',self.model.knowledge_qa.call_args.args[1])
    def test_usage_off_local_recall_no_leak(self):
        self.save();self.s.refresh_from_db();self.s.enabled=False;self.s.save()
        r=self.qa('你记得我保存过哪些情况吗？',self.fresh());self.assertEqual(r.status_code,200,r.data)
        self.assertIn('开关',r.data['answer']);self.assertNotIn('糖尿病',r.data['answer']);self.graph.assert_not_called()
    def test_guest_recall_is_local_and_requires_login(self):
        self.client.force_authenticate(None)
        r=self.client.post('/api/ai/knowledge-qa/',{'query':'你记得我保存过哪些情况吗？'},format='json')
        self.assertEqual(r.status_code,200,r.data);self.assertIn('登录',r.data['answer']);self.graph.assert_not_called()
    def test_family_recall_not_self(self):
        self.save();r=self.qa('你记得我妈妈保存过哪些情况吗？',self.fresh())
        self.assertEqual(r.status_code,200,r.data);self.assertIn('家属',r.data['answer']);self.assertNotIn('糖尿病',r.data['answer'])
    def test_no_match_is_local_does_not_claim_absence_of_disease(self):
        r=self.qa('你记得我保存过哪些情况吗？',self.fresh());self.assertEqual(r.status_code,200,r.data)
        self.assertIn('不代表',r.data['answer']);self.graph.assert_not_called()

    def test_confirmed_update_preserves_old_event_time_reference_not_old_state(self):
        row=self.save('记住，我上周感冒了。')
        result=self.qa('记住，我的感冒已经好了。');self.assertEqual(result.status_code,200,result.data)
        p=self.client.get('/api/memory/').data['health_proposals'][0]
        result=self.client.post(f"/api/memory/health/proposals/{p['id']}/confirm/",{
            'profile_version':p['profile_version'],'replace_id':row.id,'target_version':row.version},format='json')
        self.assertEqual(result.status_code,200,result.data)
        result=self.qa('我之前保存的上周感冒是什么时候？',self.fresh());self.assertEqual(result.status_code,200,result.data)
        self.assertEqual(result.data['memory']['used_health'],[{'id':row.id,'version':2}])
        self.assertIn('用户报告已结束',result.data['answer']);self.assertIn('旧来源',result.data['answer'])
    def test_unconfirmed_time_not_inherited(self):
        row=self.save('记住，我有糖尿病。')
        self.qa('记住，我去年糖尿病已经好了。')
        result=self.qa('我去年的糖尿病有什么资料？',self.fresh());self.assertEqual(result.status_code,200,result.data)
        self.assertEqual(result.data['memory']['used_health'],[])
    def test_recall_question_does_not_write_preference_and_reads_saved_value(self):
        from .models import UserMemory
        self.qa('以后简短点。');before=list(UserMemory.objects.values())
        self.save();r=self.qa('你记得我以前说以后详细回答吗？',self.fresh())
        self.assertEqual(r.status_code,200,r.data);self.assertEqual(list(UserMemory.objects.values()),before)
        self.assertIn('简短',r.data['answer']);self.assertNotIn('糖尿病',r.data['answer'])
        self.assertEqual(r.data['memory']['automatic_processing']['status'],'recall_read_only')
        self.graph.assert_not_called()
    def test_local_recall_version_change_discards_stale_result(self):
        row=self.save()
        def changed(user,q,h):
            result=context_health(user,q,h)
            self.client.post(f'/api/memory/health/{row.id}/',{'action':'pause','version':1},format='json')
            return result
        with patch('api.views.context_health',side_effect=changed):
            result=self.qa('你记得我保存过哪些情况吗？',self.fresh())
        self.assertEqual(result.status_code,409,result.data)
    def test_missing_prior_time_source_stops_time_inheritance(self):
        row=self.save('记住，我上周感冒了。');old_mid=row.report['source']['message_id']
        self.qa('记住，我的感冒已经好了。');p=self.client.get('/api/memory/').data['health_proposals'][0]
        result=self.client.post(f"/api/memory/health/proposals/{p['id']}/confirm/",{
            'profile_version':p['profile_version'],'replace_id':row.id,'target_version':1},format='json')
        self.assertEqual(result.status_code,200,result.data)
        ChatMessage.objects.filter(pk=int(old_mid[3:])).update(content='已经变更的原话')
        result=self.qa('我之前保存的上周感冒是什么时候？',self.fresh())
        self.assertEqual(result.status_code,200,result.data);self.assertEqual(result.data['memory']['used_health'],[])
    def test_mixed_recall_types_clarify_without_reading_either_content(self):
        self.save();result=self.qa('你记得我保存的糖尿病和回答偏好吗？',self.fresh())
        self.assertEqual(result.status_code,200,result.data);self.assertEqual(result.data['memory']['used_health'],[])
        self.assertEqual(result.data['memory']['selection']['status'],'mixed_recall_types');self.graph.assert_not_called()
    def test_unique_saved_reference_reaches_graph_and_answer_query(self):
        row=self.save();result=self.qa('我之前记住的那个病的预防资料是什么？',self.fresh())
        self.assertEqual(result.status_code,200,result.data)
        self.assertEqual(result.data['memory']['used_health'],[{'id':row.id,'version':1}])
        self.assertIn('糖尿病',self.graph.call_args.args[0]);self.assertIn('糖尿病',self.model.knowledge_qa.call_args.args[0])
        self.assertEqual(result.data['query'],'我之前记住的那个病的预防资料是什么？')
