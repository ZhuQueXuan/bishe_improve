"""R5 same-author scripted intent and response tests, not semantic evaluation."""
import copy
import hashlib
import json
import unittest

from .natural_memory_contract import NaturalMemoryContractError, capture_context
from .natural_memory_retrieval import (INTENT, ANSWER, routing_request, validate_intent, answer_request, validate_answer)
from .test_natural_memory_policy import Harness, preference
from .test_natural_memory_contract import disease

GRANTS = {'version': 1, 'cloud_routing': True, 'cloud_answer_personal_context': True}


def evidence():
    body = '合成专业资料：此内容仅用于工程接线检查，不构成临床建议。'
    return [{'ref': 'E1', 'title': 'R5虚构资料', 'text': body, 'source': {'document_id': 'fixture-doc',
        'version_id': 'fixture-v1', 'page': 1, 'text_sha256': hashlib.sha256(body.encode()).hexdigest()}}]


def intent(source, purpose='personal_application', *, names=None, scope='current', raw_time=None, prefs=None, grants=None):
    job = routing_request(source, grants or GRANTS)
    return json.dumps({'contract': INTENT, 'request_ref': job['request_ref'], 'purpose': purpose,
        'subject': {'personal_application': 'self', 'memory_review': 'self', 'general_knowledge': 'none',
            'other_person': 'other', 'unclear': 'unknown'}[purpose],
        'target': {'mode': ('exact_names' if names else 'all_saved') if purpose in {'personal_application', 'memory_review'} else 'none',
            'names': names or []}, 'time': {'scope': scope, 'raw': raw_time}, 'current_preferences': prefs or [],
        'evidence': source['messages'][-1]['content']}, ensure_ascii=False)


def answer(request, body='这是人工回答替身，保留用户自述的限定。'):
    return json.dumps({'contract': ANSWER, 'request_ref': request['request_ref'], 'answer': body,
        'personal_refs': [x['ref'] for x in request['payload']['personal_reports']],
        'professional_refs': [x['ref'] for x in request['payload']['professional_evidence']]}, ensure_ascii=False)


def packet(h, source, raw=None, *, docs=None, grants=None):
    return answer_request(h.state, h.policy, source, grants or GRANTS, raw or intent(source), evidence() if docs is None else docs,
        live_sources=h.live, conversation_versions=h.versions)


class NaturalMemoryRetrievalTests(unittest.TestCase):
    def test_complete_routing_prompt_example_passes_actual_contract(self):
        h = Harness(); source = h.source('结合我的情况说明这份资料。')
        request = routing_request(source, GRANTS)
        schema = json.loads(request['messages'][0]['content'].split('输出规则与示例JSON：', 1)[1])
        example = schema['format_example']; example['request_ref'] = request['request_ref']
        result = validate_intent(json.dumps(example, ensure_ascii=False), source, GRANTS)
        self.assertEqual(result['purpose'], 'personal_application')
        self.assertEqual(request['prompt_version'], 'natural-memory-query-intent-prompt-v5')

    def test_current_preference_prompt_example_passes_full_runtime_contract(self):
        h=Harness();source=h.source('这次请用通俗话解释。')
        job=routing_request(source,GRANTS)
        schema=json.loads(job['messages'][0]['content'].split('输出规则与示例JSON：',1)[1])
        example=schema['current_preference_format_example'];example['request_ref']=job['request_ref']
        actual=validate_intent(json.dumps(example,ensure_ascii=False),source,GRANTS)
        self.assertEqual(actual['current_preferences'],[{'slot':'style','value':'plain','quote':'这次请用通俗话解释。'}])

    def test_historical_context_for_current_question_keeps_past_uncertain_report(self):
        h = Harness(); s = h.source('我去年可能有银屑病。')
        c = disease(s['messages'][-1], certainty='possible'); c['time'] = {'raw':'去年','temporal_scope':'past'}
        h.stage(s, [c]); h.accept()
        s = h.source('参考我以往的记录解释今天的资料。', session='conversation:2')
        result = packet(h, s, intent(s, scope='all'))
        report = result['payload']['personal_reports'][0]
        self.assertEqual(report['event_time'], {'raw':'去年','temporal_scope':'past'})
        self.assertEqual(report['certainty'], 'possible')
        self.assertFalse(report['clinical_confirmation'])

    def test_complete_answer_prompt_example_passes_actual_contract(self):
        h = Harness(); source = h.source('请回顾我的已保存情况。')
        request = packet(h, source, intent(source, 'memory_review'), docs=[])
        example = json.loads(request['messages'][0]['content'].split('完整JSON格式例：', 1)[1])
        example['request_ref'] = request['request_ref']
        result = validate_answer(json.dumps(example, ensure_ascii=False), request)
        self.assertEqual(result['personal_refs'], [])
        self.assertFalse(result['clinical_confirmation'])
        self.assertEqual(request['prompt_version'], 'natural-memory-answer-prompt-v3')

    def rejects(self, code, fn, *args, **kw):
        with self.assertRaises(NaturalMemoryContractError) as caught:
            fn(*args, **kw)
        self.assertEqual(str(caught.exception), code)

    def test_nameless_second_session_gets_saved_self_report(self):
        h = Harness(); h.saved(); s = h.source('结合我之前说的情况，这份资料要关注什么？', session='conversation:2')
        result = packet(h, s)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['payload']['personal_reports'][0]['name_raw'], '银屑病')
        self.assertNotIn('银屑病', s['messages'][-1]['content'])
        self.assertEqual(result['selection']['selected'][0]['reason'], 'self_context_without_repeated_name')

    def test_general_knowledge_and_family_exclude_self_health(self):
        for purpose in ['general_knowledge', 'other_person']:
            h = Harness(); h.saved(); s = h.source('请介绍一下这份资料。', session='conversation:2')
            result = packet(h, s, intent(s, purpose))
            self.assertEqual(result['payload']['personal_reports'], [])
            self.assertNotIn('银屑病', json.dumps(result['messages'], ensure_ascii=False))

    def test_possible_and_negative_reports_are_preserved_in_review(self):
        h = Harness(); s = h.source('我可能有银屑病，但没有湿疹。')
        h.stage(s, [disease(s['messages'][-1], certainty='possible'), disease(s['messages'][-1], value='湿疹', polarity='negated')]); h.accept()
        s = h.source('回顾我保存的情况，是否已经确诊？', session='conversation:2')
        result = packet(h, s, intent(s, 'memory_review', scope='all'), docs=[])
        reports = result['payload']['personal_reports']
        self.assertEqual(reports[0]['certainty'], 'possible'); self.assertEqual(reports[1]['polarity'], 'negated')
        self.assertTrue(all(r['user_confirmed'] and not r['clinical_confirmation'] for r in reports))

    def test_negative_not_used_as_current_condition(self):
        h = Harness(); s = h.source('我没有银屑病。'); h.stage(s, [disease(s['messages'][-1], polarity='negated')]); h.accept()
        s = h.source('结合我的情况说明资料。', session='conversation:2'); result = packet(h, s)
        self.assertEqual(result['payload']['personal_reports'], [])
        self.assertTrue(result['payload']['limitations']['missing_personal_background_is_not_no_disease'])

    def test_past_or_resolved_only_selected_for_review_or_explicit_time(self):
        h = Harness(); s = h.source('我去年有银屑病。'); c = disease(s['messages'][-1]); c['time'] = {'raw': '去年', 'temporal_scope': 'past'}
        h.stage(s, [c]); h.accept(); s = h.source('参考去年银屑病的自述。', session='conversation:2')
        self.assertEqual(packet(h, s)['payload']['personal_reports'], [])
        explicit = packet(h, s, intent(s, names=['银屑病'], scope='explicit', raw_time='去年'))
        self.assertEqual(len(explicit['payload']['personal_reports']), 1)

    def test_unknown_time_does_not_silently_ignore_constraint(self):
        h = Harness(); h.saved(); s = h.source('回顾我很久之前的情况。', session='conversation:2')
        result = packet(h, s, intent(s, 'memory_review', scope='explicit', raw_time='很久之前'))
        self.assertEqual(result['status'], 'needs_clarification'); self.assertNotIn('messages', result)

    def test_unconfirmed_disputed_paused_forgotten_excluded(self):
        h = Harness(); h.stage(h.source()); s = h.source('结合我的情况说明。', session='conversation:2')
        self.assertEqual(packet(h, s)['payload']['personal_reports'], [])
        for action in ['pause', 'retraction', 'forget']:
            with self.subTest(action=action):
                h = Harness(); h.saved(); h.change(action); s = h.source('结合我的情况说明。', session='conversation:2')
                self.assertEqual(packet(h, s)['payload']['personal_reports'], [])

    def test_source_edit_invalidates_saved_background(self):
        h = Harness(); h.saved(); h.live['db:1']['content'] = '说的是我父亲。'
        s = h.source('结合我的情况说明。', session='conversation:2')
        self.assertEqual(packet(h, s)['payload']['personal_reports'], [])

    def test_use_off_is_independent_of_collection(self):
        h = Harness(); h.saved(); h.policy['use_enabled'] = False
        s = h.source('结合我的情况说明。', session='conversation:2')
        self.assertEqual(packet(h, s)['payload']['personal_reports'], [])
        self.assertEqual(h.policy['collect_types'], ['self_disease_report', 'presentation_preference'])

    def test_collection_off_does_not_imply_use_off(self):
        h = Harness(); h.saved(); h.policy['collect_types'] = []
        s = h.source('结合我的情况说明。', session='conversation:2')
        self.assertEqual(len(packet(h, s)['payload']['personal_reports']), 1)

    def test_two_same_name_events_require_choice_not_latest_wins(self):
        h = Harness(); h.saved(); s = h.source('我再次有银屑病。'); c = disease(s['messages'][-1])
        h.stage(s, [c], choices=h.choose(s, c, 'new_event')); h.accept()
        s = h.source('结合我的情况说明。', session='conversation:2')
        self.assertEqual(packet(h, s)['status'], 'needs_clarification')
        self.assertEqual(len(packet(h, s, intent(s, 'memory_review', scope='all'))['payload']['personal_reports']), 2)

    def test_current_preference_overrides_without_mutating_saved_default(self):
        h = Harness(); s = h.source('以后简短一些。'); h.stage(s, [preference(s)]); h.accept()
        old = copy.deepcopy(h.state)
        s = h.source('这次详细说明。', session='conversation:2')
        result = packet(h, s, intent(s, 'general_knowledge', prefs=[{'slot': 'length', 'value': 'detailed', 'quote': '这次详细说明。'}]))
        self.assertEqual(result['payload']['presentation_preferences'], [{'slot': 'length', 'value': 'detailed', 'origin': 'current_message'}])
        self.assertEqual(h.state, old)

    def test_shared_full_quote_not_sent_with_selected_preference_or_single_disease(self):
        h = Harness(); s = h.source('我有银屑病，另有湿疹，以后简短。')
        h.stage(s, [disease(s['messages'][-1]), disease(s['messages'][-1], value='湿疹'), preference(s)]); h.accept()
        s = h.source('银屑病对我有什么影响？', session='conversation:2')
        result = packet(h, s, intent(s, names=['银屑病']))
        serialized = json.dumps(result['messages'], ensure_ascii=False)
        self.assertNotIn('湿疹', serialized); self.assertNotIn('以后简短', serialized)
        self.assertEqual(len(result['payload']['personal_reports']), 1)

    def test_router_never_receives_saved_records_or_adjacent_private_context(self):
        h = Harness(); h.saved(); prior = h.live['db:1']; s = h.source('结合之前的情况说明。', context=[prior])
        serialized = json.dumps(routing_request(s, GRANTS)['messages'], ensure_ascii=False)
        self.assertNotIn('银屑病', serialized); self.assertNotIn('user:1', serialized)

    def test_personal_cloud_grant_independent_and_default_denial(self):
        h = Harness(); h.saved(); s = h.source('结合我的情况说明。', session='conversation:2')
        denied = {**GRANTS, 'cloud_answer_personal_context': False}
        result = packet(h, s, intent(s, grants=denied), grants=denied)
        self.assertEqual(result['status'], 'personal_answer_permission_required'); self.assertNotIn('messages', result)

    def test_bad_intent_extra_fields_wrong_refs_and_fake_names_rejected(self):
        h = Harness(); s = h.source('结合我的情况说明。')
        for key, value, code in [('owner', 'user:2', 'intent_fields'), ('request_ref', 'bad', 'stale_intent'),
                ('subject', 'other', 'intent_subject')]:
            obj = json.loads(intent(s)); obj[key] = value
            self.rejects(code, validate_intent, json.dumps(obj), s, GRANTS)
        self.rejects('intent_name_not_in_question', validate_intent, intent(s, names=['编造病名']), s, GRANTS)

    def test_professional_evidence_is_separate_and_hash_checked(self):
        h = Harness(); h.saved(); s = h.source('结合我的情况说明。', session='conversation:2')
        result = packet(h, s)
        self.assertEqual(len(result['payload']['personal_reports']), 1); self.assertEqual(len(result['payload']['professional_evidence']), 1)
        bad = evidence(); bad[0]['text'] += 'changed'
        self.rejects('professional_text_changed', packet, h, s, docs=bad)

    def test_no_professional_evidence_cannot_generate_medical_application(self):
        h = Harness(); h.saved(); s = h.source('结合我的情况说明。', session='conversation:2')
        self.assertEqual(packet(h, s, docs=[])['status'], 'no_professional_evidence')

    def test_final_answer_refs_are_strict_not_semantic_proof(self):
        h = Harness(); h.saved(); s = h.source('结合我的情况说明。', session='conversation:2'); request = packet(h, s)
        self.assertEqual(validate_answer(answer(request), request)['semantic_assurance'], 'generated_not_medically_verified')
        obj = json.loads(answer(request)); obj['personal_refs'] = ['M999']
        self.rejects('answer_references', validate_answer, json.dumps(obj), request)
        # A structurally valid answer can still make a wrong clinical assertion.
        result = validate_answer(answer(request, '这是故意错误的确诊表述。'), request)
        self.assertFalse(result['clinical_confirmation'])

    def test_complete_message_budget_not_just_evidence_text(self):
        h = Harness(); s = h.source('介绍资料。'); result = packet(h, s, intent(s, 'general_knowledge'))
        self.assertEqual(result['input_bytes'], len(json.dumps(result['messages'], ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()))

    def test_critical_dispute_never_used_as_certain_background(self):
        h = Harness(); h.saved(); s = h.source('我没有银屑病。')
        h.stage(s, [disease(s['messages'][-1], polarity='negated')])
        s = h.source('结合我的情况说明。', session='conversation:2')
        result = packet(h, s)
        self.assertEqual(result['payload']['personal_reports'], [])
        self.assertIn('entry_disputed', [x['reason'] for x in result['selection']['excluded']])

    def test_changed_query_source_cannot_reuse_old_intent_snapshot(self):
        h = Harness(); h.saved(); s = h.source('结合我的情况说明。', session='conversation:2')
        h.live[s['current_message_id']]['content'] = '改问我父亲。'
        self.rejects('source_changed', packet, h, s)

    def test_more_than_five_reports_is_clarified_without_truncation(self):
        h = Harness()
        for index in range(6):
            name = '合成疾病' + str(index); s = h.source('我患有' + name + '。')
            h.stage(s, [disease(s['messages'][-1], value=name)]); h.accept()
        s = h.source('回顾我保存的全部情况。', session='conversation:2')
        result = packet(h, s, intent(s, 'memory_review', scope='all'))
        self.assertEqual(result['status'], 'needs_clarification'); self.assertNotIn('messages', result)

    def test_full_payload_over_budget_is_rejected_before_answer_call(self):
        h = Harness(); s = h.source('介绍资料。'); docs = []
        for index in range(8):
            row = evidence()[0]; row['ref'] = 'E' + str(index + 1); row['text'] = '合' * 1900
            row['source']['text_sha256'] = hashlib.sha256(row['text'].encode()).hexdigest(); docs.append(row)
        self.rejects('model_message_content', packet, h, s, intent(s, 'general_knowledge'), docs=docs)
