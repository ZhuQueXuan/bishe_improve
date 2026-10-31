"""Source intent and locally rendered missing notices; no paid calls."""
from unittest.mock import Mock,patch
from django.test import SimpleTestCase,override_settings
from rest_framework.test import APITestCase
from .answer_boundaries import neutralize_missing_details,MISSING_NOTICE
from .chat_runtime import intent_names,prepare_context,source_catalog,ChatAgent
from .test_agent_loop import fact,found


class IntentBoundaryTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass(); cls.catalog=source_catalog()
    def context(self,history,query='它需要注意什么？'):
        return prepare_context([{'role':r,'content':t} for r,t in history],query,'test',self.catalog)[0]
    def test_actual_literal_collision_with_plural_cue(self):
        self.assertEqual(self.context([('user','我想了解糖尿病和高血压这两种疾病。')]).snapshot()['objects'],['糖尿病','高血压'])
    def test_discussion_clause_plural_can_override_longest_name(self):
        self.assertEqual(self.context([('user','讨论糖尿病和高血压，分别介绍这两种疾病。')]).snapshot()['objects'],['糖尿病','高血压'])
    def test_plural_cue_after_discussion_sentence(self):
        self.assertEqual(self.context([('user','讨论糖尿病和高血压。这两种疾病先了解一下。')]).snapshot()['objects'],['糖尿病','高血压'])
    def test_literal_composite_without_plural_cue_is_preserved(self):
        self.assertEqual(self.context([('user','讨论糖尿病和高血压。')]).snapshot()['objects'],['糖尿病和高血压'])
    def test_explicit_single_literal_wins_over_negated_plural(self):
        self.assertEqual(self.context([('user','讨论一个病名叫糖尿病和高血压的条目，不是两种疾病。')]).snapshot()['objects'],['糖尿病和高血压'])
    def test_parent_disease_does_not_split_out_of_specific_name(self):
        self.assertEqual(intent_names('妊娠糖尿病是什么？',['糖尿病','妊娠糖尿病']),['妊娠糖尿病'])
    def test_specific_name_remains_longest_inside_plural_items(self):
        self.assertEqual(intent_names('妊娠糖尿病和高血压这两种疾病',['糖尿病','妊娠糖尿病','高血压']),['妊娠糖尿病','高血压'])
    def test_assistant_cannot_introduce_plural_intent(self):
        m=self.context([('user','讨论糖尿病。'),('assistant','糖尿病和高血压这两种疾病分别讨论。')])
        self.assertEqual(m.snapshot()['objects'],['糖尿病'])
    def test_choose_one_after_clarification_resolves_to_latest_user(self):
        m=self.context([('user','我想了解糖尿病和高血压这两种疾病。'),('assistant','你想查哪一种？')],query='我指高血压。')
        self.assertEqual(m.snapshot()['objects'],['高血压'])
    def test_correction_does_not_reintroduce_earlier_disease(self):
        m=self.context([('user','讨论糖尿病，用表格。'),('user','取消表格。换个话题，改查高血压。')])
        self.assertEqual(m.snapshot()['objects'],['高血压']); self.assertIsNone(m.snapshot()['format'])


class MissingNoticeTests(SimpleTestCase):
    def test_heading_replaces_unverified_examples_but_keeps_core_quote(self):
        text='肾功能良好者可选择高钾低钠盐[E1]。\n\n**需注意的缺失**：未提供禁忌，如高钾血症、ACEI/ARB。'
        answer,policy=neutralize_missing_details(text)
        self.assertIn('[E1]',answer); self.assertNotIn('ACEI',answer); self.assertNotIn('高钾血症',answer)
        self.assertTrue(policy['replaced']); self.assertTrue(answer.endswith(MISSING_NOTICE))
    def test_entire_insufficiency_response_becomes_fixed_notice(self):
        text='当前资料未收录注意事项，只有食物条目[E1]。\n\n应结合血钾和用药进行评估。'
        self.assertEqual(neutralize_missing_details(text)[0],MISSING_NOTICE)
    def test_title_does_not_hide_an_insufficiency_only_response(self):
        text='**注意事项**\n\n当前资料没有该内容[E1]。\n\n可举例某个未检索的药物。'
        self.assertEqual(neutralize_missing_details(text)[0],MISSING_NOTICE)
    def test_unheaded_terminal_missing_paragraph_replaced(self):
        text='来源中明确的人群限制[E1]。\n\n本次资料未涉及药物X与药物Y的相互作用。'
        answer,_=neutralize_missing_details(text)
        self.assertIn('[E1]',answer); self.assertNotIn('药物X',answer)
    def test_heading_variants_all_use_same_local_notice(self):
        for label in ['缺失说明','资料边界说明','信息不足之处','资料缺失与说明']:
            for heading in [f'**{label}**：缺少某药的剂量。',f'### {label}\n没有禁忌清单。',f'{label}：未检索例子。']:
                self.assertEqual(neutralize_missing_details('原文[E1]。\n\n'+heading)[0],'原文[E1]。\n\n'+MISSING_NOTICE)
    def test_limitation_subheading_cannot_restore_removed_details(self):
        text='原文[E1]。\n\n缺失说明：\n未提供详情\n\n**例如**\n未检索药物X'
        self.assertNotIn('药物X',neutralize_missing_details(text)[0])
    def test_evidence_based_population_restriction_is_not_removed(self):
        text='**适用人群**\n\n肾功能良好者可选择高钾低钠盐；肾功能不全者补钾前咨询医生[E1]。'
        self.assertEqual(neutralize_missing_details(text)[0],text)
    def test_source_conflict_comparison_is_not_a_missing_section(self):
        text='**差异说明**\n\n图谱6g[E1]，指南5g[E2]，两者不一致。'
        self.assertEqual(neutralize_missing_details(text)[0],text)
    def test_limitations_guard_does_not_claim_full_semantic_validation(self):
        text='任意正文中额外出现药物X[E1]。'
        answer,policy=neutralize_missing_details(text)
        self.assertEqual(answer,text); self.assertIn('not_full_semantic',policy['scope'])
    def test_agent_citation_validation_still_precedes_notice_rendering(self):
        model=Mock(); model.plan_query.side_effect=ValueError('offline')
        model.knowledge_qa.return_value='当前资料未覆盖该细节[E999]。'
        a=ChatAgent(Mock(return_value=found(fact())),Mock(),lambda:model,query='糖尿病症状')
        r=a.retrieve('糖尿病症状',[])
        self.assertIsNone(a.answer('糖尿病症状',r,[])); model.knowledge_qa.assert_called_once()
    def test_accepted_insufficiency_is_rendered_without_another_call(self):
        model=Mock(); model.plan_query.side_effect=ValueError('offline')
        model.knowledge_qa.return_value='当前资料未覆盖该细节[E1]。\n\n建议评估未检索的药物X。'
        a=ChatAgent(Mock(return_value=found(fact())),Mock(),lambda:model,query='糖尿病症状')
        r=a.retrieve('糖尿病症状',[])
        self.assertEqual(a.answer('糖尿病症状',r,[]),MISSING_NOTICE)
        model.knowledge_qa.assert_called_once(); self.assertTrue(a.trace['answer_boundary_policy']['replaced'])

    def test_uncited_insufficiency_becomes_only_local_notice(self):
        model=Mock(); model.plan_query.side_effect=ValueError('offline')
        model.knowledge_qa.return_value='现有资料只列出食物，未覆盖该问题。\n\n**缺失说明：** 未提供具体注意事项。'
        a=ChatAgent(Mock(return_value=found(fact())),Mock(),lambda:model,query='糖尿病症状')
        r=a.retrieve('糖尿病症状',[])
        self.assertEqual(a.answer('糖尿病症状',r,[]),MISSING_NOTICE)
        model.knowledge_qa.assert_called_once()

    def test_uncited_substantive_answer_still_fails(self):
        for text in ['此药每天一片。','原文给出了某人群限制。\n\n缺失说明：未提供剂量。']:
            model=Mock(); model.plan_query.side_effect=ValueError('offline'); model.knowledge_qa.return_value=text
            a=ChatAgent(Mock(return_value=found(fact())),Mock(),lambda:model,query='糖尿病症状')
            r=a.retrieve('糖尿病症状',[])
            self.assertIsNone(a.answer('糖尿病症状',r,[]))

    def test_uncited_insufficiency_with_unknown_id_still_fails(self):
        model=Mock(); model.plan_query.side_effect=ValueError('offline')
        model.knowledge_qa.return_value='当前资料未覆盖该细节。\n\n缺失说明：未提供注意事项[E999]。'
        a=ChatAgent(Mock(return_value=found(fact())),Mock(),lambda:model,query='糖尿病症状')
        r=a.retrieve('糖尿病症状',[])
        self.assertIsNone(a.answer('糖尿病症状',r,[]))


class ClarificationAPITests(APITestCase):
    def test_explicit_two_diseases_no_model_or_tool_calls_in_both_modes(self):
        for enabled in [False,True]:
            with override_settings(DOCUMENT_RAG_ENABLED=enabled),patch('api.views.retrieve_context') as graph,patch('api.views.DeepSeekAPI') as model,patch('api.views.DocumentSearch') as docs:
                response=self.client.post('/api/ai/knowledge-qa/',{'query':'它需要注意什么？','history':[
                    {'id':'h1','role':'user','content':'我想了解糖尿病和高血压这两种疾病。'}]},format='json')
                self.assertEqual(response.status_code,200); self.assertEqual(response.data['retrieval']['status'],'ambiguous')
                self.assertIn('明确',response.data['answer']); self.assertFalse(response.data['has_context'])
                graph.assert_not_called(); model.assert_not_called(); docs.return_value.preview.assert_not_called()
