"""R1 structure/provenance tests only. Never opens reserved acceptance content."""
import copy
import json
from pathlib import Path
import unittest

from .natural_memory_contract import (capture_context, validate_candidate, validate_response,
    check_model_messages_budget, content_hash, NaturalMemoryContractError, CANDIDATE_CONTRACT,
    RESPONSE_CONTRACT, MAX_REQUEST_BYTES)


def envelope(text='我患有银屑病。', **changes):
    return {'id': 'db:5', 'owner': 'user:1', 'session': 'conversation:1', 'role': 'user',
        'content': text, 'recorded_at': '2026-10-28T10:00:00+08:00', **changes}


def snapshot(text='我患有银屑病。', **kw):
    return capture_context(envelope(text), owner='user:1', session='conversation:1',
        policy_version=1, conversation_version=0, collect_types=['self_disease_report', 'presentation_preference'], **kw)


def disease(source, value='银屑病', quote=None, *, source_ref='current', **content_changes):
    return {'contract': CANDIDATE_CONTRACT, 'category': 'self_disease_report', 'subject': 'self',
        'content': {'value_raw': value, 'polarity': 'affirmed', 'certainty': 'asserted', **content_changes},
        'time': {'raw': None, 'temporal_scope': 'unspecified'}, 'relation': 'new_information',
        'evidence': [{'source_ref': source_ref, 'text': source['content'] if quote is None else quote}]}


def checked(candidate, source, **changes):
    return validate_candidate(candidate, source, expected_policy_version=source['policy_version'],
        expected_conversation_version=source['conversation_version'],
        expected_context_digest=source['context_digest'], **changes)


def response(items):
    return json.dumps({'contract': RESPONSE_CONTRACT, 'items': items}, ensure_ascii=False)


def parse(raw, source):
    return validate_response(raw, source, expected_policy_version=source['policy_version'],
        expected_conversation_version=source['conversation_version'], expected_context_digest=source['context_digest'])


class NaturalMemoryContractTests(unittest.TestCase):
    def rejects(self, code, fn, *args, **kw):
        with self.assertRaises(NaturalMemoryContractError) as caught:
            fn(*args, **kw)
        self.assertEqual(str(caught.exception), code)

    def test_open_name_and_paraphrases_need_no_local_literal_parser(self):
        for phrase in ['我患有银屑病。', '银屑病是我自己的情况。', '门诊说我得了银屑病。']:
            s = snapshot(phrase); result = checked(disease(s['messages'][-1]), s)
            self.assertEqual(result['candidate']['content']['value_raw'], '银屑病')
            self.assertFalse(result['writable']); self.assertTrue(result['requires_semantic_validation'])

    def test_polarity_certainty_and_unknown_are_separate_dimensions(self):
        for phrase, polarity, certainty in [('我可能有银屑病。', 'affirmed', 'possible'),
                ('我可能没有银屑病。', 'negated', 'possible'), ('银屑病有没有我也不知道。', 'unknown', 'unknown')]:
            s = snapshot(phrase); c = disease(s['messages'][-1], polarity=polarity, certainty=certainty)
            self.assertEqual(checked(c, s)['candidate']['content'], c['content'])

    def test_source_time_is_not_ingestion_or_diagnosis_confirmation(self):
        s = snapshot('我去年患有银屑病。'); c = disease(s['messages'][-1])
        c['time'] = {'raw': '去年', 'temporal_scope': 'past'}
        result = checked(c, s)
        self.assertEqual(result['groundings'][0]['recorded_at'], s['messages'][-1]['recorded_at'])
        self.assertEqual(result['candidate']['time']['raw'], '去年')
        self.assertFalse(result['user_confirmed']); self.assertFalse(result['clinical_confirmation'])

    def test_multiple_user_evidence_and_full_context_are_retained(self):
        old = envelope('我以前生病的那次需要补充。', id='db:2')
        assistant = envelope('请继续说明。', id='db:3', role='assistant')
        s = snapshot('那次是银屑病。', context=[old, assistant]); c = disease(s['messages'][-1])
        earlier = s['messages'][0]
        c['evidence'].insert(0, {'source_ref': 'context_1', 'text': earlier['content']})
        result = checked(c, s)
        self.assertEqual(len(result['groundings']), 2)
        self.assertEqual(result['semantic_context'], s['messages'])

    def test_chinese_emoji_offsets_are_server_located(self):
        s = snapshot('🙂 我患有银屑病。'); c = disease(s['messages'][-1], quote='我患有银屑病')
        result = checked(c, s)
        self.assertEqual((result['groundings'][0]['start'], result['groundings'][0]['end']), (2, 8))

    def test_snapshot_and_result_are_detached(self):
        s = snapshot(); c = disease(s['messages'][-1]); before = copy.deepcopy(s)
        result = checked(c, s); result['semantic_context'][0]['content'] = 'edited'
        result['candidate']['content']['value_raw'] = 'edited'
        self.assertEqual(s, before); self.assertEqual(c['content']['value_raw'], '银屑病')

    def test_privilege_and_confirmation_fields_cannot_be_model_supplied(self):
        s = snapshot()
        for key, value in [('owner', 'user:9'), ('user_confirmed', True), ('clinical_confirmation', True),
                ('writable', True), ('target_key', 'other'), ('origin', 'user_confirmation'), ('normalized_name', '高血压')]:
            c = disease(s['messages'][-1]); c[key] = value
            self.rejects('candidate_fields', checked, c, s)

    def test_unknown_subject_or_deferred_category_is_not_self_candidate(self):
        s = snapshot()
        for subject in ['family', 'other', 'unknown', None, []]:
            c = disease(s['messages'][-1]); c['subject'] = subject
            self.rejects('subject_requires_clarification_or_out_of_scope', checked, c, s)
        c = disease(s['messages'][-1]); c['category'] = 'current_medication'
        self.rejects('category', checked, c, s)

    def test_explicit_collection_choice_not_old_auto_enabled(self):
        s = capture_context(envelope(), owner='user:1', session='conversation:1', policy_version=0,
            conversation_version=0, collect_types=[])
        self.rejects('collection_disabled', checked, disease(s['messages'][-1]), s)

    def test_cross_account_session_role_and_reset_rejected(self):
        for changed, code in [({'owner': 'user:2'}, 'source_partition'),
                ({'session': 'conversation:2'}, 'source_partition'), ({'role': 'assistant'}, 'current_not_user')]:
            self.rejects(code, capture_context, envelope(**changed), owner='user:1', session='conversation:1',
                policy_version=1, conversation_version=0, collect_types=['self_disease_report'])
        self.rejects('source_order_or_reset', snapshot, source_after_id=5)
        self.rejects('source_excluded', snapshot, excluded_ids=['db:5'])

    def test_assistant_or_absent_message_cannot_donate_evidence(self):
        s = snapshot('是。', context=[envelope('你有银屑病。', id='db:2', role='assistant')])
        c = disease(s['messages'][0], source_ref='context_1')
        self.rejects('evidence_not_owned_user_source', checked, c, s)
        c = disease(s['messages'][-1]); c['evidence'][0]['source_ref'] = 'context_9'
        self.rejects('evidence_not_owned_user_source', checked, c, s)

    def test_current_user_evidence_is_mandatory(self):
        s = snapshot('补充一点。', context=[envelope(id='db:2')])
        self.rejects('current_user_evidence_required', checked, disease(s['messages'][0], source_ref='context_1'), s)

    def test_partial_context_never_becomes_complete_source(self):
        s = snapshot(context_complete=False)
        self.rejects('context_incomplete', checked, disease(s['messages'][-1]), s)

    def test_hash_binds_full_message_and_later_correction(self):
        s = snapshot('我患有银屑病。说的是我自己。'); c = disease(s['messages'][-1], quote='我患有银屑病')
        mutated = copy.deepcopy(s); mutated['messages'][-1]['content'] += '后续更正。'
        self.rejects('source_hash_changed', checked, c, mutated)
        c['evidence'][0]['message_hash'] = '0' * 64
        self.rejects('evidence_fields', checked, c, s)

    def test_context_digest_cannot_be_rebased_under_expected_snapshot(self):
        s = snapshot(); c = disease(s['messages'][-1]); changed = snapshot('我自己患有银屑病。')
        self.rejects('context_changed', validate_candidate, c, changed, expected_policy_version=1,
            expected_conversation_version=0, expected_context_digest=s['context_digest'])

    def test_policy_and_conversation_versions_checked_again(self):
        s = snapshot(); c = disease(s['messages'][-1])
        self.rejects('stale_policy', validate_candidate, c, s, expected_policy_version=2,
            expected_conversation_version=0, expected_context_digest=s['context_digest'])
        self.rejects('stale_conversation', validate_candidate, c, s, expected_policy_version=1,
            expected_conversation_version=1, expected_context_digest=s['context_digest'])

    def test_quotes_missing_ambiguous_or_duplicated_are_not_repaired(self):
        s = snapshot(); c = disease(s['messages'][-1], quote='我得的是银屑病')
        self.rejects('quote_missing', checked, c, s)
        s = snapshot('银屑病，银屑病。'); c = disease(s['messages'][-1], quote='银屑病')
        self.rejects('quote_ambiguous', checked, c, s)
        c = disease(s['messages'][-1]); c['evidence'] *= 2
        self.rejects('duplicate_evidence', checked, c, s)

    def test_invented_entity_and_time_or_normalization_are_rejected(self):
        s = snapshot(); c = disease(s['messages'][-1], value='糖尿病')
        self.rejects('value_not_in_user_evidence', checked, c, s)
        c = disease(s['messages'][-1]); c['time'] = {'raw': '昨天', 'temporal_scope': 'past'}
        self.rejects('time_not_in_user_evidence', checked, c, s)
        c['time'] = {'raw': None, 'temporal_scope': 'past'}
        self.rejects('time_basis_required', checked, c, s)
        c['time'] = {'raw': None, 'temporal_scope': 'unspecified', 'start': '2026-01-01'}
        self.rejects('time_fields', checked, c, s)
        c = disease(s['messages'][-1], dose='1mg')
        self.rejects('disease_fields', checked, c, s)

    def test_invalid_types_and_non_json_numbers_are_static_errors(self):
        s = snapshot()
        for key, value, code in [('polarity', ['affirmed'], 'polarity'), ('certainty', True, 'certainty')]:
            c = disease(s['messages'][-1]); c['content'][key] = value
            self.rejects(code, checked, c, s)
        self.rejects('policy_version', capture_context, envelope(), owner='user:1', session='conversation:1',
            policy_version=True, conversation_version=0, collect_types=[])
        self.rejects('non_json_number', parse, '{"contract":"x","items":[NaN]}', s)

    def test_context_count_byte_order_and_unicode_limits(self):
        self.rejects('context_count', snapshot, context=[envelope(id='db:1')] * 5)
        self.rejects('source_order_or_reset', snapshot, context=[envelope(id='db:6')])
        self.rejects('message_budget', snapshot, text='中' * 1366)
        self.rejects('invalid_unicode', snapshot, text='\ud800')
        self.rejects('context_budget', snapshot, context=[envelope('中' * 700, id='db:1'), envelope('中' * 700, id='db:2')])

    def test_response_rejects_duplicates_extra_fields_truncation_and_size(self):
        s = snapshot()
        self.rejects('duplicate_json_key', parse, '{"contract":"x","items":[],"items":[]}', s)
        self.rejects('response_fields', parse, json.dumps({'contract': RESPONSE_CONTRACT, 'items': [], 'writable': True}), s)
        self.rejects('invalid_response_json', parse, '{"items":[', s)
        self.rejects('response_budget', parse, ' ' * 8193, s)
        self.rejects('candidate_count', parse, response([disease(s['messages'][-1])] * 6), s)

    def test_nested_duplicate_fields_and_incomplete_authority_envelopes(self):
        s = snapshot(); raw = response([disease(s['messages'][-1])])
        raw = raw.replace('"polarity": "affirmed"', '"polarity": "affirmed", "polarity": "negated"')
        self.rejects('duplicate_json_key', parse, raw, s)
        msg = envelope(); msg.pop('owner')
        self.rejects('message_fields', capture_context, msg, owner='user:1', session='conversation:1',
            policy_version=1, conversation_version=0, collect_types=[])

    def test_new_packets_cannot_be_accepted_as_legacy_candidates(self):
        from .personal_memory_contract import validate_candidate as legacy_validate
        from .memory_contract import MemoryContractError
        s = snapshot(); new = checked(disease(s['messages'][-1]), s)
        for packet in [new, new['candidate']]:
            with self.assertRaises(MemoryContractError) as caught:
                legacy_validate(packet, {})
            self.assertEqual(str(caught.exception), 'candidate_fields')

    def test_local_rule_origin_never_grants_authority(self):
        s = snapshot(); result = checked(disease(s['messages'][-1]), s, origin='local_rule')
        self.assertEqual(result['origin'], 'local_rule')
        self.assertFalse(result['writable']); self.assertFalse(result['user_confirmed'])
        self.rejects('origin', checked, disease(s['messages'][-1]), s, origin='user_confirmation')

    def test_model_references_have_no_database_ids_or_server_hashes(self):
        s = snapshot(); c = disease(s['messages'][-1]); result = checked(c, s)
        self.assertEqual(set(c['evidence'][0]), {'source_ref', 'text'})
        self.assertEqual(result['groundings'][0]['message_id'], 'db:5')
        self.assertEqual(result['groundings'][0]['message_hash'], s['messages'][-1]['message_hash'])
        c['evidence'][0]['source_ref'] = []
        self.rejects('evidence_source_ref', checked, c, s)

    def test_response_atomicity_and_effective_duplicate_check(self):
        s = snapshot(); c = disease(s['messages'][-1]); bad = copy.deepcopy(c); bad['subject'] = 'family'
        original = copy.deepcopy(s)
        self.rejects('subject_requires_clarification_or_out_of_scope', parse, response([c, bad]), s)
        self.rejects('duplicate_candidate', parse, response([c, c]), s)
        self.assertEqual(s, original)

    def test_empty_response_is_structural_only_not_negative_success(self):
        result = parse(response([]), snapshot())
        self.assertEqual(result['candidates'], [])
        self.assertTrue(result['requires_semantic_validation']); self.assertFalse(result['writable'])

    def test_preference_values_and_declared_scope_only(self):
        s = snapshot('以后请把话说得容易懂一些。'); c = disease(s['messages'][-1])
        c.update(category='presentation_preference', content={'slot': 'style', 'value': 'plain', 'scope': 'long_term'},
            time={'raw': None, 'temporal_scope': 'not_applicable'})
        self.assertEqual(checked(c, s)['candidate']['content']['value'], 'plain')
        c['content']['scope'] = 'current'
        self.rejects('preference_not_long_term', checked, c, s)
        c['content'].update(slot='background', value='nursing', scope='long_term')
        self.rejects('preference_value', checked, c, s)

    def test_structural_pass_never_claims_to_understand_negation_or_subject(self):
        for phrase in ['我没有银屑病。', '我患有银屑病。其实说的是我父亲。']:
            s = snapshot(phrase)
            # Deliberately wrong meaning in a physically grounded synthetic item.
            result = checked(disease(s['messages'][-1]), s)
            self.assertEqual(result['validation_level'], 'structural_only')
            self.assertTrue(result['requires_semantic_validation'])
            self.assertFalse(result['writable']); self.assertFalse(result['writes_performed'])
            self.assertEqual(result['semantic_context'][-1]['content'], phrase)

    def test_complete_model_request_budget_includes_structure_and_prompt(self):
        self.assertEqual(check_model_messages_budget([{'role': 'user', 'content': '中'}]),
            len(json.dumps([{'role': 'user', 'content': '中'}], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()))
        self.rejects('complete_request_budget', check_model_messages_budget,
            [{'role': 'system', 'content': 'x' * (MAX_REQUEST_BYTES - 20)}])
        self.rejects('model_role', check_model_messages_budget, [{'role': [], 'content': 'x'}])

    def test_preview_identity_is_stable_but_changes_with_source(self):
        s = snapshot(); c = disease(s['messages'][-1])
        self.assertEqual(checked(c, s)['preview_id'], checked(copy.deepcopy(c), s)['preview_id'])
        s2 = snapshot('我自己患有银屑病。')
        self.assertNotEqual(checked(c, s)['preview_id'], checked(disease(s2['messages'][-1]), s2)['preview_id'])

    def test_development_gold_items_are_structurally_representable_not_extracted(self):
        path = Path(__file__).resolve().parents[2] / 'data/natural_memory_v1/r0_development_cases.json'
        data = json.loads(path.read_text(encoding='utf-8')); represented = 0
        for case in data['single_cases']:
            if not case['expected']['facts']:
                continue
            mapped = {}; originals = case['messages']
            for i, m in enumerate(originals, 1):
                mapped[m['id']] = envelope(m['content'], id='db:' + str(i), role=m['role'], recorded_at=m['sent_at'])
            current = mapped[case['current_message_id']]
            s = capture_context(current, owner='user:1', session='conversation:1', policy_version=1,
                conversation_version=0, collect_types=['self_disease_report', 'presentation_preference'],
                context=[mapped[m['id']] for m in originals if m['id'] != case['current_message_id']])
            proposals = []
            for gold in case['expected']['facts']:
                if gold['category'] == 'self_disease_report':
                    content = {k: gold[k] for k in ['value_raw', 'polarity', 'certainty']}
                    tm = {'raw': gold['time_raw'], 'temporal_scope': gold['temporal_scope']}
                else:
                    content = {k: gold[k] for k in ['slot', 'value', 'scope']}
                    tm = {'raw': None, 'temporal_scope': 'not_applicable'}
                prior_ids = [m['id'] for m in originals if m['id'] != case['current_message_id']]
                evidence = [{'source_ref': 'current' if e['message_id'] == case['current_message_id']
                    else 'context_' + str(prior_ids.index(e['message_id']) + 1), 'text': e['text']} for e in gold['evidence']]
                proposals.append({'contract': CANDIDATE_CONTRACT, 'category': gold['category'], 'subject': 'self',
                    'content': content, 'time': tm, 'relation': 'new_information', 'evidence': evidence})
            results = parse(response(proposals), s)
            represented += len(results['candidates'])
            self.assertTrue(all(not c['writable'] for c in results['candidates']))
        self.assertEqual(represented, 12)
