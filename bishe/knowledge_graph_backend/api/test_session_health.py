"""Finite raw-language stage B and actual production API input regressions."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock,patch
from django.test import SimpleTestCase,override_settings
from django.contrib.auth.models import User
from rest_framework.test import APITestCase
from .chat_runtime import ChatMemory,prepare_context,source_catalog,ChatAgent,intent_names
from .chat_runtime import Checkpoints,RUNTIME_ROOT
from .planning_tools import SnapshotTools
from .planning_runtime import entity_id
from .planning_policies import MockDynamicPolicy
import uuid
from .session_health import SessionHealth
from .models import Conversation,ChatMessage,UserMemory
from .planning_runtime import BoundaryError
from .conversation_context import build_context
from .test_agent_loop import fact,found


def message(mid,text,role='user',session='s'):
    return {'id':mid,'role':role,'content':text,'session_id':session}


class SessionHealthTests(SimpleTestCase):
    def setUp(self):self.m=ChatMemory('s',['感冒','咳嗽','糖尿病','高血压'])
    def add(self,i,text,role='user'):self.m.ingest(message(str(i),text,role))
    def effective(self,subject,name):return self.m.health.effective[(subject,name)]

    def test_predeclared_raw_language_cases(self):
        path=Path(__file__).resolve().parents[2]/'data/memory_system_v1/stageB_acceptance.json'
        cases=json.loads(path.read_text(encoding='utf-8'))['cases']
        seen=[]
        for case in cases:
            with self.subTest(case=case['id']):
                self.setUp()
                for i,text in enumerate(case['inputs']):self.add(i,text)
                health=self.m.health;cur=health.current;state=self.m.snapshot();code=case['id'];seen.append(code)
                if code=='B01':self.assertEqual(set(health.effective),{('self','糖尿病'),('family:mother','糖尿病')})
                elif code=='B02':
                    self.assertEqual(self.effective('self','感冒')['event_state'],'resolved')
                    self.assertEqual(state['objects'],['咳嗽']);self.assertNotIn('感冒',cur['query'])
                elif code=='B03':self.assertEqual(state['objects'],['高血压']);self.assertNotIn('糖尿病',cur['query'])
                elif code in {'B04','B05'}:self.assertEqual(self.effective('self','感冒')['event_state'],'uncertain')
                elif code in {'B06','B08','B09','B20','B21','B22'}:
                    self.assertIsNotNone(health.clarification(state['objects']))
                elif code=='B07':
                    self.assertEqual(self.effective('self','糖尿病')['event_state'],'denied')
                    self.assertFalse(health.pending);self.assertEqual(len(health.records),2)
                elif code=='B10':
                    self.assertEqual(self.effective('self','感冒')['event_state'],'current')
                    self.assertEqual(health.records[-1]['event_state'],'past')
                elif code=='B11':
                    self.assertEqual(self.effective('self','感冒')['event_state'],'current')
                    self.assertNotEqual(health.records[-1]['event_id'],health.records[-2]['event_id'])
                    self.assertEqual(health.records[-2]['event_state'],'resolved')
                elif code in {'B12','B13'}:self.assertEqual(health.records,[])
                elif code in {'B14','B15','B18'}:self.assertNotIn('health_context',self.m.state_packet())
                elif code=='B16':
                    self.assertEqual(state['objects'],['感冒'])
                    self.assertEqual(self.m.state_packet()['health_context']['records'][0]['event_state'],'resolved')
                elif code=='B17':self.assertIsNone(health.clarification(state['objects']))
                elif code=='B19':self.assertEqual(self.m.state_packet()['health_context']['records'][0]['subject'],'family:mother')
                elif code=='B23':self.assertIsNone(health.clarification(state['objects']));self.assertEqual(health.audit[-1]['reason'],'compatible_observation_no_episode_merge')
                elif code=='B24':self.assertEqual(len(health.records),1)
                elif code=='B25':self.assertEqual(self.effective('self','糖尿病')['event_state'],'current')
                else:self.fail('Scenario requires an explicit assertion')
        self.assertEqual(len(seen),25)

    def test_original_offsets_recorded_time_and_episode_link_are_visible(self):
        self.add(1,'我上周感冒了。');self.add(2,'我那次感冒已经好了。');self.add(3,'我那次感冒的预防资料有哪些？')
        packet=self.m.state_packet()['health_context'];row=packet['records'][0]
        self.assertEqual(row['event_state'],'resolved')
        self.assertTrue(packet['event_links']);self.assertEqual(len(packet['supporting_reports_not_current_state']),1)
        for entry in packet['records']+packet['supporting_reports_not_current_state']:
            s=entry['source'];original=self.m.messages[s['message_id']]['content']
            self.assertEqual(original[s['start']:s['end']],s['text'])
            self.assertIsNone(entry['event_time']['start']);self.assertIsNone(entry['event_time']['end'])
        self.assertIn('original_message_time_unknown',packet['source_time_basis']['2'])

    def test_inherited_subject_does_not_inherit_wrong_event_time(self):
        self.add(1,'我上周感冒已经好了，现在咳嗽。')
        self.assertEqual(self.effective('self','咳嗽')['event_time']['raw'],'现在')
        self.assertIn('我',self.effective('self','咳嗽')['source']['text'])

    def test_stored_original_timestamp_uses_original_time_basis(self):
        item=message('1','我有糖尿病。');item['recorded_at']='2026-10-20T11:00:00+08:00';self.m.ingest(item)
        self.assertEqual(self.effective('self','糖尿病')['recorded_at'],item['recorded_at'])
        self.assertEqual(self.m.state_packet()['health_context']['source_time_basis']['1'],'stored_message_time')

    def test_historical_question_selects_matching_episode_not_current(self):
        self.add(1,'我去年感冒了。');self.add(2,'我今天又感冒了。')
        self.add(3,'我去年感冒的预防资料有哪些？')
        selected=self.m.state_packet()['health_context']['records']
        self.assertEqual(len(selected),1);self.assertEqual(selected[0]['event_state'],'past')
        self.assertEqual(selected[0]['event_time']['raw'],'去年')

    def test_very_long_binding_source_clarifies_instead_of_raising_500(self):
        self.add(1,'更正，'+('说明'*850)+'，我没有糖尿病。')
        self.assertIsNotNone(self.m.health.current['clarification'])
        self.assertEqual(self.m.health.records,[])

    def test_explicit_new_time_cannot_close_wrong_episode(self):
        self.add(1,'我今天感冒了。');self.add(2,'我去年感冒已经好了。')
        self.assertEqual(self.effective('self','感冒')['event_state'],'current')
        self.assertNotEqual(self.m.health.records[0]['event_id'],self.m.health.records[1]['event_id'])

    def test_absolute_date_is_not_silently_interpreted_as_current(self):
        self.add(1,'我2020年感冒了。')
        record=self.effective('self','感冒')
        self.assertEqual(record['event_state'],'uncertain');self.assertEqual(record['event_time']['raw'],'2020年')
        self.assertIsNone(record['event_time']['start'])

    def test_now_recovered_refers_to_state_change_not_new_illness_time(self):
        self.add(1,'我上周感冒了。');self.add(2,'我现在感冒已经好了。')
        self.assertEqual(self.effective('self','感冒')['event_state'],'resolved')
        self.assertEqual(self.m.health.records[0]['event_id'],self.m.health.records[1]['event_id'])

    def test_recovery_of_other_past_period_does_not_end_undated_current_report(self):
        self.add(1,'我感冒了。');self.add(2,'我去年感冒已经好了。')
        self.assertEqual(self.effective('self','感冒')['event_state'],'current')

    def test_explicit_reference_with_conflicting_dates_clarifies(self):
        self.add(1,'我今天感冒了。');self.add(2,'我上周那次感冒已经好了。')
        self.assertIsNotNone(self.m.health.current['clarification'])
        self.assertEqual(self.effective('self','感冒')['event_state'],'current')

    def test_unrelated_statement_does_not_automatically_end_old_report(self):
        self.add(1,'我有糖尿病。');self.add(2,'最近没问题了。')
        self.assertEqual(self.effective('self','糖尿病')['event_state'],'current')

    def test_uncertain_recovery_question_does_not_change_state(self):
        self.add(1,'我感冒了。');self.add(2,'我感冒已经好了么？')
        self.assertEqual(self.effective('self','感冒')['event_state'],'current')

    def test_double_negation_is_not_flattened_to_denial(self):
        for text in ['我不是没有糖尿病。','我并非没有糖尿病。']:
            self.setUp();self.add(1,text)
            self.assertIsNotNone(self.m.health.current['clarification'])
            self.assertEqual(self.m.health.records,[])

    def test_subjectless_recovery_is_not_resolved_using_last_person_alone(self):
        self.add(1,'我感冒了。');self.add(2,'我妈妈感冒了。');self.add(3,'感冒好了。')
        self.assertTrue(all(r['event_state']=='current' for r in self.m.health.effective.values()))

    def test_hope_and_generic_nonmedical_improvement_do_not_close_health_event(self):
        for text in ['希望已经好了。','学习效果已经好了。','我希望感冒已经好了。']:
            self.setUp();self.add(1,'我感冒了。');self.add(2,text)
            self.assertEqual(self.effective('self','感冒')['event_state'],'current')

    def test_duplicate_and_foreign_or_assistant_messages_do_not_update(self):
        row=message('1','我有糖尿病。');self.m.ingest(row);self.m.ingest(row)
        self.add(2,'我没有糖尿病。','assistant')
        self.assertFalse(self.m.ingest(message('3','我没有糖尿病。',session='another')))
        self.assertEqual(len(self.m.health.records),1)

    def test_source_registry_cannot_override_original_user_message(self):
        self.add(1,'我有糖尿病。')
        self.m.health.sources['1']['content']='我有高血压。'
        row=self.effective('self','糖尿病');row['source'].update(text='我有高血压。',end=len('我有高血压。'))
        with self.assertRaisesRegex(BoundaryError,'health_state_source'):self.m.state_packet()

    def test_mandatory_budget_rejects_whole_health_state(self):
        self.add(1,'我有糖尿病。')
        self.assertEqual(build_context(self.m,'1',128)['status'],'context_limit')
        p=build_context(self.m,'1',12000)
        self.assertLessEqual(p['input_bytes'],12000);self.assertIn('health_context',p['payload'])

    def test_filtered_dialogue_preserves_question_but_not_superseded_self_report(self):
        self.add(1,'我上周感冒了。');self.add(2,'我那次感冒已经好了，咳嗽是什么？')
        history=[{'id':'1','role':'user','content':'我上周感冒了。'},
                 {'id':'a1','role':'assistant','content':'旧助手推断。'},
                 {'id':'2','role':'user','content':'我那次感冒已经好了，咳嗽是什么？'}]
        filtered=self.m.health.dialogue_history(history)
        text=json.dumps(filtered,ensure_ascii=False)
        self.assertNotIn('旧助手推断',text);self.assertNotIn('感冒',text)
        self.assertIn('咳嗽是什么',text)

    def make_paused_task(self):
        query='糖尿病的并发疾病中，选前2个，列出描述和预防资料，用表格。'
        catalog=source_catalog();tools=Mock(wraps=SnapshotTools(catalog,[entity_id('糖尿病')]))
        checkpoints=Checkpoints(RUNTIME_ROOT/('stageB-'+uuid.uuid4().hex))
        model=Mock();model.plan_query.side_effect=ValueError('offline')
        class Planner(MockDynamicPolicy):
            def __init__(self,model):super().__init__();self.records=[]
        def agent(text,history,**kwargs):
            return ChatAgent(Mock(return_value=found(fact())),Mock(),lambda:model,query=text,history=history,
                session_id='s',owner={'user':None,'session':'s'},catalog=catalog,tools=tools,
                checkpoints=checkpoints,planner_factory=Planner,**kwargs)
        history=[{'id':'current-question-before-save','role':'user','content':'我有糖尿病。'}]
        paused=agent(query,history,pause_after_steps=1).retrieve(query,history)
        return query,history,paused,agent,tools

    def test_pause_restore_rejects_changed_report_before_reading_tools(self):
        query,history,paused,agent,tools=self.make_paused_task()
        changed=history+[{'id':'task','role':'user','content':query},
                         {'id':'correction','role':'user','content':'更正，我没有糖尿病。'}]
        count=tools.read.call_count
        with self.assertRaisesRegex(BoundaryError,'task_expired_invalid_or_used'):
            agent('继续整理',changed,resume_token=paused['resume_token']).retrieve('继续整理',changed)
        self.assertEqual(tools.read.call_count,count)

    def test_unchanged_report_can_resume_after_database_message_id_assignment(self):
        query,history,paused,agent,tools=self.make_paused_task()
        persisted=[{**history[0],'id':'db:assigned-id'},{'id':'task','role':'user','content':query}]
        resumed=agent('继续整理',persisted,resume_token=paused['resume_token']).retrieve('继续整理',persisted)
        self.assertEqual(resumed['task']['status'],'complete')


@override_settings(DOCUMENT_RAG_ENABLED=False)
class SessionHealthAPITests(APITestCase):
    def setUp(self):
        self.user=User.objects.create_user(username='stageB-synthetic')
        self.c=Conversation.objects.create(user=self.user);self.client.force_authenticate(self.user)
        self.model=Mock();self.model.plan_query.side_effect=ValueError('offline')
        self.model.knowledge_qa.return_value='离线资料[E1]'
        self.graph=Mock(return_value=found(fact(),fact('desc','离线资料说明。')))
        for item in [patch('api.views.DeepSeekAPI',return_value=self.model),patch('api.views.retrieve_context',self.graph),
                     patch('api.views.retrieve_phrase',return_value=found(fact()))]:item.start();self.addCleanup(item.stop)
        self.n=0
    def qa(self,q,c=None):
        self.n+=1
        r=self.client.post('/api/ai/knowledge-qa/',{'query':q,'conversation_id':(c or self.c).id,'client_message_id':str(self.n)},format='json')
        self.assertEqual(r.status_code,200,r.data);return r.data
    def packet(self):return json.loads(self.model.knowledge_qa.call_args.args[1].splitlines()[1])

    def test_recovery_filters_retrieval_and_answer_only_for_health_normalization(self):
        self.qa('我上周感冒了。')
        self.qa('我那次感冒已经好了，现在咳嗽。')
        self.assertNotIn('感冒',self.graph.call_args.args[0]);self.assertNotIn('感冒',self.model.knowledge_qa.call_args.args[0])
        health=self.packet()['state']['health_context']
        self.assertEqual([r['value'] for r in health['records']],['咳嗽'])
        self.assertNotIn('感冒',json.dumps(self.packet().get('dialogue_reference',{}),ensure_ascii=False))

    def test_ordinary_pronoun_keeps_original_answer_query_contract(self):
        self.qa('糖尿病是什么？');self.qa('它有哪些症状？')
        self.assertEqual(self.model.knowledge_qa.call_args.args[0],'它有哪些症状？')
        self.assertIn('糖尿病',self.graph.call_args.args[0])

    def test_denied_disease_not_sent_to_graph_or_understanding_or_answer(self):
        self.qa('我没有糖尿病，我想了解高血压。')
        self.assertNotIn('糖尿病',self.graph.call_args.args[0])
        self.assertNotIn('糖尿病',self.model.plan_query.call_args.args[1])
        self.assertNotIn('糖尿病',self.model.knowledge_qa.call_args.args[0])

    def test_relevant_conflict_is_local_and_sources_preserved(self):
        self.qa('我有糖尿病。');self.graph.reset_mock();self.model.reset_mock()
        data=self.qa('我没有糖尿病。')
        self.assertEqual(data['retrieval']['status'],'ambiguous');self.assertIn('更正',data['answer'])
        self.graph.assert_not_called();self.model.knowledge_qa.assert_not_called()
        self.assertEqual(len(data['memory']['short']['health_context']['conflicting_reports']),1)

    def test_explicit_correction_local_ack_keeps_chat_but_not_UserMemory(self):
        self.qa('我有糖尿病。');self.graph.reset_mock()
        data=self.qa('更正，我没有糖尿病。')
        self.assertEqual(data['retrieval']['status'],'health_ack');self.graph.assert_not_called()
        self.assertEqual(self.c.messages.filter(role='user').count(),2);self.assertEqual(UserMemory.objects.count(),0)

    def test_past_and_resolved_only_updates_do_not_call_models(self):
        self.qa('我上周感冒了。');self.qa('我那次感冒已经好了。')
        self.graph.assert_not_called();self.model.knowledge_qa.assert_not_called();self.model.plan_query.assert_not_called()

    def test_family_question_has_family_report_not_self(self):
        self.qa('我妈妈有糖尿病。');self.qa('它的预防资料有哪些？')
        rows=self.packet()['state']['health_context']['records']
        self.assertEqual({r['subject'] for r in rows},{'family:mother'})

    def test_cache_has_no_persisted_health_records(self):
        self.qa('我有糖尿病。')
        self.c.refresh_from_db()
        text=json.dumps(self.c.context_notes,ensure_ascii=False)
        self.assertNotIn('health_context',text);self.assertNotIn('health:糖尿病',text)
        self.assertEqual(UserMemory.objects.count(),0)

    def test_new_conversation_and_reset_do_not_import_session_reports(self):
        self.qa('我妈妈有糖尿病。')
        other=Conversation.objects.create(user=self.user)
        self.assertNotIn('health_context',self.qa('糖尿病是什么？',other)['memory']['short'])
        self.client.post(f'/api/conversations/{self.c.id}/memory/',{'action':'reset'},format='json')
        self.assertNotIn('health_context',self.qa('糖尿病是什么？')['memory']['short'])

    def test_cross_account_cannot_use_report_history(self):
        self.qa('我妈妈有糖尿病。');other=User.objects.create_user(username='stageB-other')
        self.client.force_authenticate(other)
        response=self.client.post('/api/ai/knowledge-qa/',{'query':'它的症状有哪些？','conversation_id':self.c.id},format='json')
        self.assertEqual(response.status_code,404);self.assertEqual(UserMemory.objects.count(),0)

    def test_inflight_conversation_update_discards_old_answer_without_saving_it(self):
        self.qa('我有糖尿病。')
        before=self.c.messages.count()
        def update_during_answer(*args,**kwargs):
            ChatMessage.objects.create(conversation=self.c,role='user',content='更正，我没有糖尿病。')
            return '旧状态的模拟回答[E1]'
        self.model.knowledge_qa.side_effect=update_during_answer
        response=self.client.post('/api/ai/knowledge-qa/',{'query':'它的症状有哪些？','conversation_id':self.c.id,'client_message_id':'inflight'},format='json')
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.c.messages.count(),before+1)
        self.assertFalse(self.c.messages.filter(content__contains='旧状态的模拟回答').exists())
