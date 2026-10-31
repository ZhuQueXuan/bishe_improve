"""R3 synthetic state decisions, not natural-language or production acceptance."""
import copy
import json
import unittest

from .natural_memory_contract import (NaturalMemoryContractError, CANDIDATE_CONTRACT,
    capture_context, validate_candidate, digest)
from .natural_memory_policy import (POLICY, new_state, propose, confirm, resolve, manage,
    eligible_entries, entry_display_digest)
from .test_natural_memory_contract import envelope, disease
from .test_natural_memory_extraction import args, extraction, review, question


class Harness:
    def __init__(self):
        self.state = new_state('user:1')
        self.policy = {'contract': POLICY, 'owner': 'user:1', 'version': 1,
            'collect_types': ['self_disease_report', 'presentation_preference'],
            'use_enabled': True, 'preference_auto_enabled': False, 'preference_auto_gate_passed': False}
        self.live = {}; self.versions = {'conversation:1': 0}; self.latest = {}; self.serial = 0

    def source(self, body='我患有银屑病。', *, session='conversation:1', context=()):
        self.serial += 1
        message = envelope(body, id='db:' + str(self.serial), session=session)
        self.versions.setdefault(session, 0); self.live[message['id']] = message
        self.latest[session] = message['id']
        return capture_context(message, owner='user:1', session=session, policy_version=self.policy['version'],
            conversation_version=self.versions[session], collect_types=self.policy['collect_types'],
            context=[{k: v for k, v in m.items() if k != 'message_hash'} for m in context])

    def options(self):
        return {'policy': self.policy, 'live_sources': self.live, 'conversation_versions': self.versions,
            'latest_user_ids': self.latest, 'expected_revision': self.state['revision']}

    def stage(self, source, items=None, *, choices=None, key=None, verdict='supported', coverage='complete', questions=None):
        ex = extraction(source, items=items, questions=questions)
        pure_pref = items and all(x['category'] == 'presentation_preference' for x in items) and not questions
        rv = None if pure_pref else review(source, ex, verdict=verdict, coverage=coverage)
        result = propose(self.state, source, permissions=args(source)['permissions'],
            request_key=key or 'propose-' + str(self.serial), extraction_response=ex, review_response=rv,
            choices=choices, **self.options())
        self.state = result['state']; return result

    def intent(self, display, decision='accept', *, reply=None, session='conversation:1'):
        return {'kind': 'reply' if reply else 'click', 'owner': 'user:1', 'session': session,
            'display_digest': display, 'decision': decision, 'message_id': reply}

    def accept(self, *, decision='accept', reply=None, key=None, session='conversation:1'):
        plan = self.state['pending']
        result = confirm(self.state, owner='user:1', event=self.intent(plan['display_digest'], decision,
            reply=reply, session=session), request_key=key or 'confirm-' + str(self.serial), **self.options())
        self.state = result['state']; return result

    def change(self, action, target=None, key=None):
        target = target or next(iter(self.state['entries']))
        entry = self.state['entries'][target]
        result = manage(self.state, owner='user:1', request_key=key or action + '-' + str(self.state['revision']),
            target=target, target_version=entry['version'], action=action,
            event=self.intent(entry_display_digest(entry), action), **self.options())
        self.state = result['state']; return result

    def eligible(self):
        return eligible_entries(self.state, owner='user:1', policy=self.policy,
            live_sources=self.live, conversation_versions=self.versions)

    def saved(self, body='我患有银屑病。', **kw):
        source = self.source(body, **kw); self.stage(source); self.accept(session=source['session'])
        return next(reversed(self.state['entries']))

    def choose(self, source, candidate, operation, target=None, event_state=None):
        grounded = validate_candidate(candidate, source, **{k: v for k, v in args(source).items() if k != 'permissions'})
        return {grounded['preview_id']: {'operation': operation, 'target': target,
            'target_version': self.state['entries'][target]['version'] if target else None,
            'event_state': event_state}}


def preference(source, value='brief', slot='length'):
    return {'contract': CANDIDATE_CONTRACT, 'category': 'presentation_preference', 'subject': 'self',
        'content': {'slot': slot, 'value': value, 'scope': 'long_term'},
        'time': {'raw': None, 'temporal_scope': 'not_applicable'}, 'relation': 'new_information',
        'evidence': [{'source_ref': 'current', 'text': source['messages'][-1]['content']}]}


class NaturalMemoryPolicyTests(unittest.TestCase):
    def rejects(self, code, fn, *a, **kw):
        with self.assertRaises(NaturalMemoryContractError) as caught:
            fn(*a, **kw)
        self.assertEqual(str(caught.exception), code)

    def test_health_requires_confirmation_and_preserves_possible(self):
        h = Harness(); s = h.source('我可能有银屑病。')
        result = h.stage(s, [disease(s['messages'][-1], certainty='possible')])
        self.assertEqual(result['status'], 'needs_confirmation'); self.assertEqual(h.eligible(), [])
        result = h.accept(); entry = h.eligible()[0]
        self.assertTrue(entry['user_confirmed']); self.assertFalse(entry['clinical_confirmation'])
        self.assertEqual(entry['fact']['content']['certainty'], 'possible')
        self.assertFalse(result['writes_performed']); self.assertFalse(result['saved_ack'])

    def test_health_cannot_auto_even_with_preference_gate(self):
        h = Harness(); h.policy.update(preference_auto_enabled=True, preference_auto_gate_passed=True)
        self.assertEqual(h.stage(h.source())['status'], 'needs_confirmation')

    def test_default_preference_also_waits_without_runtime_gate(self):
        h = Harness(); s = h.source('以后回答简短一些。')
        result = h.stage(s, [preference(s)])
        self.assertEqual(result['status'], 'needs_confirmation')
        self.assertEqual(h.state['pending']['semantic_assurance'], 'extraction_only_not_supported')

    def test_preference_auto_requires_both_trusted_switches(self):
        for enabled, passed, expected in [(True, False, 'needs_confirmation'), (False, True, 'needs_confirmation'),
                (True, True, 'projected_auto_preference')]:
            with self.subTest(enabled=enabled, passed=passed):
                h = Harness(); h.policy.update(preference_auto_enabled=enabled, preference_auto_gate_passed=passed)
                s = h.source('以后回答简短一些。'); self.assertEqual(h.stage(s, [preference(s)])['status'], expected)
                if expected == 'projected_auto_preference':
                    self.assertFalse(h.eligible()[0]['user_confirmed'])

    def test_typed_explicit_reply_finishes_in_one_action(self):
        h = Harness(); h.stage(h.source()); reply = h.source('确认，这条是我本人。')
        self.assertEqual(h.accept(reply=reply['current_message_id'])['status'], 'projected_confirmed')
        self.assertEqual(h.eligible()[0]['confirmation']['kind'], 'reply')

    def test_reject_does_not_create_fact(self):
        h = Harness(); h.stage(h.source()); h.accept(decision='reject')
        self.assertEqual(h.state['entries'], {}); self.assertIsNone(h.state['pending'])

    def test_wrong_user_click_rejected_without_mutating_input(self):
        h = Harness(); h.stage(h.source()); before = copy.deepcopy(h.state)
        event = h.intent(h.state['pending']['display_digest']); event['owner'] = 'user:2'
        self.rejects('intent_owner', confirm, h.state, owner='user:1', request_key='wrong', event=event, **h.options())
        self.assertEqual(before, h.state)

    def test_wrong_session_click_rejected(self):
        h = Harness(); h.stage(h.source())
        self.rejects('intent_owner', h.accept, session='conversation:2')

    def test_old_display_digest_rejected(self):
        h = Harness(); h.stage(h.source()); old = h.state['pending']['display_digest']
        h.stage(h.source('我可能有银屑病。'))
        self.rejects('intent_binding', confirm, h.state, owner='user:1', request_key='stale-display',
            event=h.intent(old), **h.options())

    def test_new_user_message_blocks_old_click(self):
        h = Harness(); h.stage(h.source()); h.source('先等等。')
        self.rejects('newer_user_message', h.accept)

    def test_reply_must_be_latest_owned_user_source(self):
        h = Harness(); h.stage(h.source()); s = h.source('确认。'); h.source('先别保存。')
        self.rejects('intent_reply_message', h.accept, reply=s['current_message_id'])

    def test_policy_version_change_blocks_confirmation(self):
        h = Harness(); h.stage(h.source()); h.policy['version'] += 1
        self.rejects('policy_changed', h.accept)

    def test_permission_revocation_without_version_bump_blocks_confirmation(self):
        h = Harness(); h.stage(h.source()); h.policy['collect_types'] = []
        self.rejects('policy_changed', h.accept)

    def test_original_source_edit_blocks_confirmation(self):
        h = Harness(); s = h.source(); h.stage(s); h.live[s['current_message_id']]['content'] = '说的是我父亲。'
        self.rejects('source_changed', h.accept)

    def test_deleted_source_blocks_confirmation(self):
        h = Harness(); s = h.source(); h.stage(s); del h.live[s['current_message_id']]
        self.rejects('source_missing', h.accept)

    def test_reset_version_blocks_confirmation(self):
        h = Harness(); h.stage(h.source()); h.versions['conversation:1'] += 1
        self.rejects('stale_conversation', h.accept)

    def test_clarification_is_not_convertible_to_save_by_accept(self):
        h = Harness(); s = h.source(); h.stage(s, questions=[question(s)])
        self.rejects('clarification_required', h.accept)
        self.assertEqual(h.eligible(), [])

    def test_clarified_new_source_can_replace_old_question_once(self):
        h = Harness(); s = h.source(); h.stage(s, questions=[question(s)])
        h.stage(h.source('我说的是我本人患有银屑病。')); h.accept()
        self.assertEqual(len(h.eligible()), 1)

    def test_same_event_duplicate_links_sources_without_new_entry(self):
        h = Harness(); key = h.saved(); s = h.source(); c = disease(s['messages'][-1])
        h.stage(s, [c], choices=h.choose(s, c, 'duplicate', key)); h.accept()
        entry = h.state['entries'][key]
        self.assertEqual(len(h.state['entries']), 1); self.assertEqual(len(entry['sources']), 2)
        self.assertEqual(entry['version'], 2); self.assertEqual(len(entry['history']), 1)

    def test_repeated_disease_does_not_assume_same_event(self):
        h = Harness(); h.saved(); result = h.stage(h.source())
        self.assertEqual(result['status'], 'needs_clarification')
        self.rejects('clarification_required', h.accept)

    def test_supplement_adds_explicit_time_and_retains_old_source(self):
        h = Harness(); key = h.saved(); s = h.source('去年我患有银屑病。'); c = disease(s['messages'][-1])
        c['time'] = {'raw': '去年', 'temporal_scope': 'past'}
        h.stage(s, [c], choices=h.choose(s, c, 'supplement', key)); h.accept()
        entry = h.state['entries'][key]
        self.assertEqual(entry['fact']['time']['raw'], '去年'); self.assertEqual(len(entry['sources']), 2)

    def test_supplement_cannot_change_assertion(self):
        h = Harness(); key = h.saved(); s = h.source('我可能有银屑病。'); c = disease(s['messages'][-1], certainty='possible')
        self.rejects('supplement_changes_assertion', h.stage, s, [c], choices=h.choose(s, c, 'supplement', key))

    def test_new_event_preserves_two_separate_event_identities(self):
        h = Harness(); old = h.saved(); s = h.source('这次我可能有银屑病。'); c = disease(s['messages'][-1], certainty='possible')
        h.stage(s, [c], choices=h.choose(s, c, 'new_event')); h.accept()
        entries = list(h.state['entries'].values())
        self.assertEqual(len(entries), 2); self.assertNotEqual(entries[0]['event_id'], entries[1]['event_id'])
        self.assertEqual(h.state['entries'][old]['status'], 'active')

    def test_same_event_update_preserves_identity_and_old_version(self):
        h = Harness(); old = h.saved(); s = h.source('医生现在说我可能有银屑病。'); c = disease(s['messages'][-1], certainty='possible')
        h.stage(s, [c], choices=h.choose(s, c, 'same_event_update', old))
        self.assertEqual(h.eligible(), []); h.accept()
        entry = h.state['entries'][old]
        self.assertEqual(entry['event_id'], old); self.assertEqual(entry['fact']['content']['certainty'], 'possible')
        self.assertEqual(entry['history'][0]['fact']['content']['certainty'], 'asserted')

    def test_resolved_typed_event_update_stops_current_background(self):
        h = Harness(); old = h.saved(); s = h.source('银屑病这次已经好了。'); c = disease(s['messages'][-1])
        h.stage(s, [c], choices=h.choose(s, c, 'same_event_update', old, 'resolved')); h.accept()
        self.assertEqual(h.state['entries'][old]['event_state'], 'resolved'); self.assertEqual(h.eligible(), [])
        self.assertEqual(h.state['entries'][old]['fact']['content']['polarity'], 'affirmed')

    def test_critical_conflict_is_disputed_before_confirm_and_stays_on_reject(self):
        h = Harness(); old = h.saved(); s = h.source('我没有银屑病。')
        h.stage(s, [disease(s['messages'][-1], polarity='negated')])
        self.assertEqual(h.state['entries'][old]['status'], 'disputed'); self.assertEqual(h.eligible(), [])
        h.accept(decision='reject'); self.assertEqual(h.eligible(), [])
        h.change('keep_original'); self.assertEqual(len(h.eligible()), 1)

    def test_correction_to_different_name_requires_explicit_target(self):
        h = Harness(); old = h.saved(); s = h.source('更正，之前说错了，我患有湿疹。'); c = disease(s['messages'][-1], value='湿疹')
        c['relation'] = 'correction'
        h.stage(s, [c], choices=h.choose(s, c, 'correction', old))
        self.assertEqual(h.eligible(), []); h.accept()
        entry = h.state['entries'][old]
        self.assertEqual(entry['fact']['content']['value_raw'], '湿疹')
        self.assertEqual(entry['history'][0]['fact']['content']['value_raw'], '银屑病')

    def test_model_relation_cannot_choose_target(self):
        h = Harness(); h.saved(); s = h.source('其实我患有湿疹。'); c = disease(s['messages'][-1], value='湿疹'); c['relation'] = 'correction'
        self.assertEqual(h.stage(s, [c])['status'], 'needs_clarification')

    def test_wrong_subject_review_quarantines_old_self_without_storing_family(self):
        h = Harness(); old = h.saved(); s = h.source('银屑病说的是我父亲，不是我。')
        self.assertEqual(h.stage(s, verdict='contradicted')['status'], 'rejected')
        self.assertEqual(h.state['entries'][old]['status'], 'disputed'); self.assertEqual(h.eligible(), [])
        self.assertEqual(len(h.state['entries']), 1)

    def test_full_context_subject_correction_has_no_valid_self_candidate(self):
        h = Harness(); s = h.source('我患有银屑病。我说的是我父亲。')
        c = disease(s['messages'][-1], quote='我患有银屑病。')
        h.stage(s, [c], verdict='contradicted'); self.assertEqual(h.state['entries'], {})

    def test_measurement_and_temporary_request_do_not_clear_disease_or_default(self):
        h = Harness(); old = h.saved(); s = h.source('以后回答简短一些。')
        h.stage(s, [preference(s)]); h.accept(); before = copy.deepcopy(h.state['entries'])
        s = h.source('今天测量正常，这次请详细讲。'); h.stage(s, [])
        self.assertEqual(before, h.state['entries']); self.assertEqual(h.state['entries'][old]['status'], 'active')

    def test_new_default_replaces_same_slot_only_after_confirm(self):
        h = Harness(); s = h.source('以后简短一些。'); h.stage(s, [preference(s)]); h.accept()
        key = next(iter(h.state['entries'])); s = h.source('以后详细一点。'); h.stage(s, [preference(s, 'detailed')])
        self.assertEqual(h.state['entries'][key]['fact']['content']['value'], 'brief')
        h.accept(); self.assertEqual(h.state['entries'][key]['fact']['content']['value'], 'detailed')
        self.assertEqual(len(h.state['entries']), 1)

    def test_pause_resume_require_explicit_current_version(self):
        h = Harness(); h.saved(); h.change('pause'); self.assertEqual(h.eligible(), [])
        h.change('resume'); self.assertEqual(len(h.eligible()), 1)

    def test_retraction_stops_background_without_claiming_fact_false(self):
        h = Harness(); key = h.saved(); h.change('retraction')
        self.assertEqual(h.eligible(), []); self.assertEqual(h.state['entries'][key]['fact']['content']['polarity'], 'affirmed')

    def test_forget_erases_all_versions_and_pending_but_keeps_chat(self):
        h = Harness(); key = h.saved(); s = h.source(); c = disease(s['messages'][-1])
        h.stage(s, [c], choices=h.choose(s, c, 'duplicate', key)); h.accept()
        h.stage(h.source()); live = copy.deepcopy(h.live); h.change('forget', key)
        self.assertNotIn('银屑病', json.dumps(h.state, ensure_ascii=False))
        self.assertIsNone(h.state['pending']); self.assertEqual(h.live, live); self.assertEqual(h.eligible(), [])

    def test_forget_works_after_collection_permission_revoked(self):
        h = Harness(); h.saved(); h.policy['collect_types'] = []; h.policy['version'] += 1
        self.assertEqual(h.change('forget')['status'], 'projected_forget')

    def test_forgotten_old_source_cannot_be_reproposed(self):
        h = Harness(); s = h.source(); h.stage(s); h.accept(); h.change('forget')
        self.rejects('source_forgotten', h.stage, s, key='late-new-key')

    def test_fresh_explicit_source_after_forget_can_create_new_entry(self):
        h = Harness(); old = h.saved(); h.change('forget'); h.stage(h.source()); h.accept()
        self.assertEqual(h.state['entries'][old]['status'], 'forgotten'); self.assertEqual(len(h.eligible()), 1)

    def test_replayed_confirmation_after_forget_never_resurrects_or_acks_save(self):
        h = Harness(); h.stage(h.source()); event = h.intent(h.state['pending']['display_digest']); options = copy.deepcopy(h.options())
        h.accept(key='unique-confirm'); h.change('forget')
        result = confirm(h.state, owner='user:1', request_key='unique-confirm', event=event, **options)
        self.assertEqual(result['status'], 'replayed'); self.assertFalse(result['saved_ack'])
        self.assertEqual(result['state'], h.state); self.assertEqual(h.eligible(), [])

    def test_identical_proposal_replay_is_metadata_only(self):
        h = Harness(); s = h.source(); before = copy.deepcopy(h.options()); ex = extraction(s); rv = review(s, ex)
        one = propose(h.state, s, permissions=args(s)['permissions'], request_key='same',
            extraction_response=ex, review_response=rv, **before)
        two = propose(one['state'], s, permissions=args(s)['permissions'], request_key='same',
            extraction_response=ex, review_response=rv, **before)
        self.assertEqual(two['status'], 'replayed'); self.assertEqual(one['state'], two['state'])

    def test_reused_key_for_different_intent_rejected(self):
        h = Harness(); h.stage(h.source(), key='repeat'); self.rejects('request_key_reused', h.stage, h.source(), key='repeat')

    def test_stale_competing_window_revision_rejected(self):
        h = Harness(); h.stage(h.source()); event = h.intent(h.state['pending']['display_digest']); old = h.options()
        h.accept()
        self.rejects('stale_state', confirm, h.state, owner='user:1', request_key='window-two', event=event, **old)

    def test_target_changed_without_plan_update_is_rejected(self):
        h = Harness(); key = h.saved(); s = h.source(); c = disease(s['messages'][-1])
        h.stage(s, [c], choices=h.choose(s, c, 'duplicate', key))
        h.state['entries'][key]['version'] += 1
        h.state['seal'] = digest({k: v for k, v in h.state.items() if k != 'seal'})  # simulated trusted concurrent edit
        self.rejects('stale_target', h.accept)

    def test_multiple_items_same_target_rejected_atomically(self):
        h = Harness(); key = h.saved(); s = h.source('我患有银屑病，也可能有银屑病。')
        a = disease(s['messages'][-1]); b = disease(s['messages'][-1], certainty='possible')
        choices = {**h.choose(s, a, 'duplicate', key), **h.choose(s, b, 'same_event_update', key)}
        before = copy.deepcopy(h.state)
        self.rejects('duplicate_target', h.stage, s, [a, b], choices=choices); self.assertEqual(h.state, before)

    def test_second_item_failure_never_partially_changes_caller_state(self):
        h = Harness(); key = h.saved(); s = h.source('我患有湿疹，也患有银屑病。')
        a = disease(s['messages'][-1], value='湿疹'); b = disease(s['messages'][-1])
        choice = h.choose(s, b, 'duplicate', key); next(iter(choice.values()))['target_version'] = 999
        before = copy.deepcopy(h.state)
        self.rejects('stale_target', h.stage, s, [a, b], choices=choice); self.assertEqual(h.state, before)

    def test_entry_sources_and_confirmation_reply_rechecked_for_eligibility(self):
        h = Harness(); h.stage(h.source()); reply = h.source('确认。'); h.accept(reply=reply['current_message_id'])
        self.assertEqual(len(h.eligible()), 1); h.live[reply['current_message_id']]['content'] = '撤回。'
        self.assertEqual(h.eligible(), [])

    def test_cross_session_qualification_and_use_switch(self):
        h = Harness(); h.saved(); h.source('继续。', session='conversation:2')
        self.assertEqual(len(h.eligible()), 1); h.policy['use_enabled'] = False
        self.assertEqual(h.eligible(), [])

    def test_state_corruption_and_model_authority_fields_rejected(self):
        h = Harness(); h.state['revision'] = 1
        self.rejects('state_integrity', h.eligible)
        h = Harness(); h.policy['model_approved'] = True
        self.rejects('policy_fields', h.eligible)

    def test_empty_or_failed_new_intake_invalidates_prior_display(self):
        h = Harness(); h.stage(h.source()); h.stage(h.source('你好。'), [])
        self.assertIsNone(h.state['pending']); self.assertEqual(h.state['entries'], {})

    def test_omission_and_unclear_semantics_cannot_be_confirmed_as_fact(self):
        h = Harness(); s = h.source(); h.stage(s, coverage='possible_omission')
        self.assertIsNone(h.state['pending']); self.assertEqual(h.state['entries'], {})
        h = Harness(); h.stage(h.source(), verdict='unclear')
        self.rejects('clarification_required', h.accept)

    def test_decision_engine_has_only_explicit_adapters(self):
        from pathlib import Path
        folder = Path(__file__).parent
        callers = [p.name for p in folder.glob('*.py') if not p.name.startswith('test_')
            and p.name not in {'natural_memory_policy.py', 'natural_memory_intake.py',
                'natural_memory_retrieval.py', 'natural_memory_answering.py', 'natural_memory_background.py'}
            and 'natural_memory_policy' in p.read_text(encoding='utf-8')]
        self.assertEqual(callers, [])

    def test_targeted_reply_resolves_same_event_without_second_confirmation(self):
        h = Harness(); old = h.saved(); ambiguous = h.source(); h.stage(ambiguous)
        s = h.source('说的是同一次银屑病，确认补充。', context=ambiguous['messages'])
        c = disease(s['messages'][-1]); ex = extraction(s, [c]); rv = review(s, ex)
        event = h.intent(h.state['pending']['display_digest'], 'resolve', reply=s['current_message_id'])
        result = resolve(h.state, s, owner='user:1', permissions=args(s)['permissions'],
            request_key='resolve-once', event=event, extraction_response=ex, review_response=rv,
            choices=h.choose(s, c, 'duplicate', old), **h.options())
        h.state = result['state']
        self.assertEqual(result['status'], 'projected_confirmed_by_resolution')
        self.assertIsNone(h.state['pending']); self.assertEqual(len(h.state['entries']), 1)
        self.assertEqual(h.state['entries'][old]['confirmation']['decision'], 'resolve')

    def test_scope_reply_can_close_question_without_changing_default(self):
        h = Harness(); s = h.source('以后简短。'); h.stage(s, [preference(s)]); h.accept()
        before = copy.deepcopy(h.state['entries'])
        ambiguous = h.source('以后详细，不过只限这一条。'); h.stage(ambiguous, [], questions=[question(ambiguous)])
        s = h.source('只是这一条详细，长期还是简短。', context=ambiguous['messages'])
        ex = extraction(s, []); rv = review(s, ex)
        event = h.intent(h.state['pending']['display_digest'], 'resolve', reply=s['current_message_id'])
        result = resolve(h.state, s, owner='user:1', permissions=args(s)['permissions'],
            request_key='resolve-scope', event=event, extraction_response=ex, review_response=rv, **h.options())
        self.assertEqual(result['status'], 'projected_resolved_without_change')
        self.assertEqual(result['state']['entries'], before); self.assertIsNone(result['state']['pending'])

    def test_resolution_cannot_silently_replace_assertion(self):
        h = Harness(); h.saved(); ambiguous = h.source(); h.stage(ambiguous)
        s = h.source('改成我患有湿疹。', context=ambiguous['messages']); c = disease(s['messages'][-1], value='湿疹')
        ex = extraction(s, [c]); rv = review(s, ex)
        event = h.intent(h.state['pending']['display_digest'], 'resolve', reply=s['current_message_id'])
        before = copy.deepcopy(h.state)
        self.rejects('resolution_changes_assertion', resolve, h.state, s, owner='user:1', permissions=args(s)['permissions'],
            request_key='resolve-changed', event=event, extraction_response=ex, review_response=rv, **h.options())
        self.assertEqual(h.state, before)

    def test_cross_round_family_reference_with_old_quote_quarantines_self(self):
        h = Harness(); old_source = h.source('我患有银屑病。'); h.stage(old_source); h.accept()
        key = next(iter(h.state['entries']))
        s = h.source('刚才说的其实是我妈妈，不是我。', context=old_source['messages'])
        c = disease(old_source['messages'][-1], source_ref='context_1')
        c['evidence'].append({'source_ref': 'current', 'text': s['messages'][-1]['content']})
        h.stage(s, [c], verdict='contradicted')
        self.assertEqual(h.state['entries'][key]['status'], 'disputed'); self.assertEqual(h.eligible(), [])
        h.change('retraction'); self.assertEqual(h.state['entries'][key]['status'], 'retracted')

    def test_two_conflicting_new_candidates_do_not_become_current_by_order(self):
        h = Harness(); s = h.source('我有银屑病，后面又说我没有银屑病。')
        h.stage(s, [disease(s['messages'][-1]), disease(s['messages'][-1], polarity='negated')])
        self.assertEqual(h.state['pending']['unresolved'], True)
        self.rejects('clarification_required', h.accept); self.assertEqual(h.eligible(), [])

    def test_target_body_drift_without_version_change_rejected(self):
        h = Harness(); key = h.saved(); s = h.source(); c = disease(s['messages'][-1])
        h.stage(s, [c], choices=h.choose(s, c, 'duplicate', key))
        h.state['entries'][key]['event_state'] = 'resolved'
        h.state['seal'] = digest({k: v for k, v in h.state.items() if k != 'seal'})
        self.rejects('target_changed', h.accept)

    def test_forget_scrubs_shared_quote_copies_in_other_entry_and_history(self):
        h = Harness(); s = h.source('我有银屑病，也有湿疹。')
        h.stage(s, [disease(s['messages'][-1]), disease(s['messages'][-1], value='湿疹')]); h.accept()
        silver, eczema = list(h.state['entries'])
        h.change('pause', eczema); h.change('resume', eczema)
        h.change('forget', silver)
        self.assertNotIn('银屑病', json.dumps(h.state, ensure_ascii=False))
        other = h.state['entries'][eczema]
        self.assertEqual(other['fact']['content']['value_raw'], '湿疹')
        self.assertEqual(other['status'], 'paused'); self.assertEqual(other['sources'], [])
        self.assertEqual(h.eligible(), [])

    def test_shared_quote_erased_entry_cannot_resume_without_new_evidence(self):
        h = Harness(); s = h.source('我有银屑病，也有湿疹。')
        h.stage(s, [disease(s['messages'][-1]), disease(s['messages'][-1], value='湿疹')]); h.accept()
        silver, eczema = list(h.state['entries']); h.change('forget', silver)
        self.rejects('fresh_evidence_required', h.change, 'resume', eczema)
