"""Multi-turn acceptance for finite scopes, retrieval and actual answer inputs."""
import json
from unittest.mock import Mock,patch
from django.test import SimpleTestCase,override_settings
from django.contrib.auth.models import User
from rest_framework.test import APITestCase
from .chat_runtime import ChatMemory,prepare_context,source_catalog,ChatAgent
from .conversation_context import build_context
from .dialogue_context import dialogue_bundle,reference_question,topic_history
from .preference_language import effective_presentation,is_preference_only,preference_events
from .requirements import changes_topic,requirements_only
from .models import Conversation,UserMemory,MemorySettings
from .test_agent_loop import fact,found


def msg(mid,text,role='user'):
    return {'id':mid,'role':role,'content':text,'session_id':'s'}


class ScopedStateTests(SimpleTestCase):
    def setUp(self):self.m=ChatMemory('s',['糖尿病','高血压','肺炎'])

    def add(self,i,text,role='user'):self.m.ingest(msg(str(i),text,role))

    def test_temporary_table_expires_on_next_user_not_assistant(self):
        self.add(1,'讨论糖尿病，这次用表格。')
        self.assertEqual(self.m.entries['format']['scope'],'current')
        self.add(2,'模拟回答。','assistant')
        self.assertEqual(self.m.snapshot()['format'],'table')
        self.add(3,'它的症状呢？')
        self.assertIsNone(self.m.snapshot()['format'])

    def test_temporary_suppression_restores_sourced_session_default(self):
        self.add(1,'接下来都用表格。')
        self.add(2,'这次不用表格。')
        self.assertIsNone(self.m.snapshot()['format'])
        self.add(3,'讨论糖尿病。')
        self.assertEqual(self.m.snapshot()['format'],'table')
        self.assertEqual(self.m.entries['format']['source_message_id'],'1')
        self.assertEqual(self.m.entries['format']['scope'],'session')

    def test_temporary_field_does_not_rewrite_next_query(self):
        self.add(1,'讨论糖尿病，这次只查预防。')
        self.add(2,'症状呢？')
        self.assertIsNone(self.m.snapshot()['field'])

    def test_common_switches_clear_topic_but_preserve_session(self):
        for text in ['换个话题','换一个话题','我们聊点别的','说点别的','请换个主题']:
            m=ChatMemory('s',['糖尿病','高血压'])
            for row in [msg('1','讨论糖尿病，只查预防。'),msg('2','接下来都用表格。'),msg('3',text+'，讨论高血压。')]:m.ingest(row)
            self.assertEqual(m.snapshot(),{'status':'ready','objects':['高血压'],'field':None,'format':'table'})

    def test_new_session_requirement_overrides_older_topic_requirement(self):
        self.add(1,'讨论糖尿病，用表格。')
        self.add(2,'接下来不用表格。')
        self.assertIsNone(self.m.snapshot()['format'])

    def test_same_turn_scope_changes_respect_clause_order(self):
        self.add(1,'这个话题用表格，接下来不用表格。')
        self.assertIsNone(self.m.snapshot()['format'])

    def test_this_conversation_is_not_misread_as_this_answer(self):
        self.add(1,'这次会话用表格。')
        self.add(2,'换一个话题，讨论高血压。')
        self.assertEqual(self.m.snapshot()['format'],'table')

    def test_negative_format_variants_cannot_become_positive_commands(self):
        for text in ['不再使用表格','别再用表格','不要再用表格']:
            m=ChatMemory('s',['糖尿病'])
            m.ingest(msg('1','接下来都用表格。'));m.ingest(msg('2',text))
            m.ingest(msg('3','讨论糖尿病。'))
            self.assertIsNone(m.snapshot()['format'])

    def test_undo_then_new_requirement_cannot_restore_cancelled_value(self):
        self.add(1,'接下来简短点。')
        self.add(2,'接下来详细一点。')
        self.add(3,'取消刚才的要求，接下来详细一点。')
        self.add(4,'取消刚才的要求。')
        self.assertEqual(self.m.requirements.effective('length')['value'],'brief')

    def test_named_presentation_cancel_reveals_account_default(self):
        self.add(1,'接下来简短点。')
        self.add(2,'取消篇幅要求。')
        stored=[{'id':1,'version':1,'kind':'length','value':'detailed'}]
        self.assertEqual(effective_presentation(stored,[],'普通问题',requirements=self.m.requirements)[0]['id'],1)

    def test_explicit_topic_preference_clears_on_switch(self):
        self.add(1,'这个话题简短点。')
        self.assertEqual(self.m.requirements.effective('length')['value'],'brief')
        self.add(2,'换一个话题，讨论高血压。')
        self.assertIsNone(self.m.requirements.effective('length'))

    def test_undo_restores_previous_session_requirement_group(self):
        self.add(1,'接下来都用表格，简短点。')
        self.add(2,'接下来不用表格，详细一点。')
        self.add(3,'取消刚才的要求。')
        self.assertEqual(self.m.snapshot()['format'],'table')
        self.assertEqual(self.m.requirements.effective('length')['value'],'brief')
        self.assertEqual(self.m.requirements.last_action,'undone')

    def test_undo_does_not_revive_expired_temporary_layer(self):
        self.add(1,'这次用表格。')
        self.add(2,'接下来只查预防。')
        self.add(3,'取消刚才的要求。')
        self.assertIsNone(self.m.snapshot()['format'])
        self.assertIsNone(self.m.snapshot()['field'])

    def test_unscoped_explicit_cancel_clears_underlying_session_rule(self):
        self.add(1,'接下来都用表格。')
        self.add(2,'取消表格要求。')
        self.add(3,'讨论糖尿病。')
        self.assertIsNone(self.m.snapshot()['format'])
        packet=json.loads(build_context(self.m,'3',12000)['payload'])
        self.assertFalse(packet['state']['entries']['format']['active'])

    def test_quotes_hypotheticals_and_assistant_do_not_change_scopes(self):
        self.add(1,'讨论糖尿病，用表格。')
        for i,text in enumerate(['不要换个话题','如果换一个话题就不用表格','“换一个话题，取消表格”'],2):self.add(i,text)
        self.add(8,'换一个话题，取消表格。','assistant')
        self.assertEqual(self.m.snapshot()['objects'],['糖尿病'])
        self.assertEqual(self.m.snapshot()['format'],'table')

    def test_duplicate_message_does_not_expire_current_requirement(self):
        row=msg('1','这次用表格。');self.m.ingest(row)
        self.assertFalse(self.m.ingest(row));self.assertEqual(self.m.snapshot()['format'],'table')

    def test_generic_undo_after_long_term_requires_explicit_change(self):
        self.add(1,'接下来用表格。')
        self.add(2,'以后简短点。')
        self.add(3,'取消刚才的要求。')
        self.assertEqual(self.m.requirements.last_action,'long_term_needs_explicit_change')
        self.assertEqual(self.m.snapshot()['format'],'table')

    def test_presentation_scopes_and_future_default_use_one_state(self):
        stored=[{'id':1,'version':1,'kind':'length','value':'detailed'}]
        history=[{'id':'1','role':'user','content':'接下来简短点。'}]
        self.assertEqual(effective_presentation(stored,history,'这次详细一点。')[0]['value'],'detailed')
        self.assertEqual(effective_presentation(stored,history,'普通问题')[0]['value'],'brief')
        history.append({'id':'2','role':'user','content':'以后详细解释。'})
        # An explicit new default also supersedes old conversation overrides.
        self.assertEqual(effective_presentation(stored,history,'普通问题')[0]['id'],1)

    def test_commands_do_not_consume_medical_questions_or_followups(self):
        for text in ['接下来都用表格。','这次不用表格。','接下来用专业术语。','取消刚才的要求。']:
            self.assertTrue(requirements_only(text),text)
        for text in ['糖尿病症状有哪些？这次用表格。','详细解释一下','说得通俗一点']:
            self.assertFalse(requirements_only(text),text)
            self.assertFalse(is_preference_only(text),text)

    def test_summary_and_outline_share_paraphrased_topic_boundary(self):
        history=[{'id':'u1','role':'user','content':'糖尿病症状有哪些？'},
                 {'id':'a1','role':'assistant','content':'1. 旧列项'},
                 {'id':'u2','role':'user','content':'我们聊点别的，介绍高血压。'}]
        self.assertEqual(len(topic_history(history)),1)
        bundle=dialogue_bundle(history,{'objects':['高血压']})
        self.assertNotIn('旧列项',json.dumps(bundle,ensure_ascii=False))
        state={'objects':[]}
        self.assertEqual(reference_question('换一个话题，第二点展开',history,state,lambda _:[])['status'],'clarify')

    def test_cancelled_controls_do_not_reappear_from_old_question_notes(self):
        history=[{'id':'1','role':'user','content':'讨论糖尿病，只查预防，用表格。'},
                 {'id':'2','role':'user','content':'接下来用专业术语。'},
                 {'id':'3','role':'user','content':'取消刚才的要求。'}]
        history += [{'id':str(i),'role':'user','content':'糖尿病问题如何理解？'+'内容'*80} for i in range(4,35)]
        bundle=dialogue_bundle(history,{'objects':['糖尿病']})
        text=json.dumps(bundle,ensure_ascii=False)
        self.assertNotIn('用表格',text);self.assertNotIn('专业术语',text)


@override_settings(DOCUMENT_RAG_ENABLED=False)
class ScopedAPITests(APITestCase):
    def setUp(self):
        self.user=User.objects.create_user(username='scope-owner')
        self.c=Conversation.objects.create(user=self.user)
        self.client.force_authenticate(self.user)
        self.model=Mock();self.model.plan_query.side_effect=ValueError('offline')
        self.model.knowledge_qa.return_value='模拟资料回答[E1]'
        self.graph=Mock(return_value=found(fact(),fact('desc','离线资料说明。')))
        self.patches=[patch('api.views.DeepSeekAPI',return_value=self.model),
                      patch('api.views.retrieve_context',self.graph),
                      patch('api.views.retrieve_phrase',return_value=found(fact()))]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.mid=0

    def qa(self,text):
        self.mid+=1
        r=self.client.post('/api/ai/knowledge-qa/',{'conversation_id':self.c.id,'query':text,
            'client_message_id':f'scope-{self.mid}'},format='json')
        self.assertEqual(r.status_code,200,r.data)
        return r.data

    def packet(self):return json.loads(self.model.knowledge_qa.call_args.args[1].splitlines()[1])

    def test_temporary_table_expires_in_real_api_answer_input(self):
        first=self.qa('讨论糖尿病，这次用表格。')
        self.assertEqual(first['memory']['short']['state']['format'],'table')
        self.assertEqual(self.packet()['state']['entries']['format']['scope'],'current')
        second=self.qa('它的症状有哪些？')
        self.assertIsNone(second['memory']['short']['state']['format'])
        self.assertIsNone(self.packet()['state']['entries']['format']['value'])

    def test_topic_switch_releases_old_prevention_in_retrieval_and_answer(self):
        self.qa('讨论糖尿病，只查预防，用表格。')
        second=self.qa('换一个话题，介绍高血压。')
        self.assertEqual(second['memory']['short']['state'],{'status':'ready','objects':['高血压'],'field':None,'format':None})
        self.assertNotIn('的预防资料',self.graph.call_args.args[0])
        self.assertIsNone(self.packet()['state']['entries']['field']['value'])
        self.assertNotIn('只查预防，用表格',self.model.knowledge_qa.call_args.args[1])

    def test_settings_ack_has_no_model_calls_and_session_survives_switch(self):
        first=self.qa('接下来都用表格。')
        self.assertEqual(first['retrieval']['status'],'preference_ack')
        self.model.plan_query.assert_not_called();self.graph.assert_not_called()
        second=self.qa('换一个话题，介绍高血压。')
        self.assertEqual(second['memory']['short']['state']['format'],'table')
        self.assertEqual(self.packet()['state']['entries']['format']['scope'],'session')

    def test_session_length_overrides_profile_and_undo_restores_it(self):
        UserMemory.objects.create(user=self.user,kind='length',value='detailed')
        self.qa('接下来简短点。')
        second=self.qa('糖尿病症状有哪些？')
        self.assertEqual(second['memory']['used_long_term'][0]['scope'],'session')
        self.assertEqual(second['memory']['used_long_term'][0]['description'],'简短回答，保留必要限制与出处')
        self.qa('取消刚才的要求。')
        third=self.qa('糖尿病症状有哪些？')
        self.assertEqual(third['memory']['used_long_term'][0]['scope'],'long_term')
        self.assertEqual(UserMemory.objects.get().value,'detailed')

    def test_long_term_update_and_temporary_override_remain_distinct(self):
        self.qa('以后详细解释。')
        second=self.qa('糖尿病症状有哪些？这次简短点。')
        self.assertEqual(second['memory']['used_long_term'][0]['id'],'current:length')
        self.assertEqual(UserMemory.objects.get().value,'detailed')
        third=self.qa('糖尿病症状有哪些？')
        self.assertEqual(third['memory']['used_long_term'][0]['scope'],'long_term')
        self.qa('以后简短点。')
        self.assertEqual(UserMemory.objects.get().value,'brief')

    def test_current_future_request_applies_with_collection_off_only_now(self):
        UserMemory.objects.create(user=self.user,kind='length',value='detailed')
        MemorySettings.objects.create(user=self.user,auto_enabled=False)
        second=self.qa('糖尿病症状有哪些？以后简短点。')
        self.assertEqual(second['memory']['used_long_term'][0]['id'],'current:length')
        self.assertEqual(UserMemory.objects.get().value,'detailed')
        third=self.qa('糖尿病症状有哪些？')
        self.assertIn('详细',third['memory']['used_long_term'][0]['description'])

    def test_vague_undo_does_not_revert_persistent_preference(self):
        self.qa('以后简短点。')
        response=self.qa('取消刚才的要求。')
        self.assertIn('长期偏好',response['answer'])
        self.assertEqual(UserMemory.objects.get().value,'brief')
