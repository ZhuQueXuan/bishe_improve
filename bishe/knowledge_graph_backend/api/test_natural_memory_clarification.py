"""Offline clarification regressions, including durable authenticated API flow.

Replies are scripted. These are not new real-model or reserved-set results.
"""
import copy
import json
import unittest
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from . import test_natural_memory_contract as ct
from . import test_natural_memory_extraction as et
from . import test_natural_memory_policy as pt
from .natural_memory_contract import NaturalMemoryContractError
from .natural_memory_extraction import preview, prepare_review
from .natural_memory_policy import resolve
from . import natural_memory_intake as intake
from .models import Conversation
from .natural_memory_models import NaturalMemoryLedger, NaturalMemoryJob


def unclear_review(source, ex, *, question_verdict='appropriate'):
    obj = json.loads(et.review(source, ex, coverage='unclear', question_verdict=question_verdict))
    obj['coverage']['reason'] = 'context_gap'
    return json.dumps(obj, ensure_ascii=False)


class ClarificationReviewTests(unittest.TestCase):
    def test_appropriate_question_waits_despite_undecided_user_meaning(self):
        source = ct.snapshot('医生说可能有高血压。')
        ex = et.extraction(source, [], [et.question(source)])
        result = et.evaluate(source, ex, unclear_review(source, ex))
        self.assertEqual(result['status'], 'needs_confirmation')
        self.assertEqual(result['coverage']['verdict'], 'unclear')
        self.assertEqual(len(result['questions']), 1)
        self.assertEqual(result['supported'], [])
        self.assertFalse(result['writable']); self.assertFalse(result['writes_performed'])

    def test_omission_remains_reassessment_even_when_a_question_is_present(self):
        source = ct.snapshot(); ex = et.extraction(source, [], [et.question(source)])
        self.assertEqual(et.evaluate(source, ex, et.review(source, ex, coverage='possible_omission'))['status'], 'needs_reassessment')

    def test_unclear_mixed_candidates_do_not_use_question_only_route(self):
        source = ct.snapshot(); ex = et.extraction(source, [ct.disease(source['messages'][-1])], [et.question(source)])
        self.assertEqual(et.evaluate(source, ex, unclear_review(source, ex))['status'], 'needs_reassessment')

    def test_unnecessary_or_unclear_question_is_not_ready_to_ask(self):
        source = ct.snapshot(); ex = et.extraction(source, [], [et.question(source)])
        for verdict in ['unnecessary', 'unclear']:
            self.assertEqual(et.evaluate(source, ex, unclear_review(source, ex, question_verdict=verdict))['status'], 'needs_reassessment')

    def test_unclear_coverage_without_question_still_reassesses(self):
        source = ct.snapshot(); ex = et.extraction(source, [])
        self.assertEqual(et.evaluate(source, ex, unclear_review(source, ex))['status'], 'needs_reassessment')

    def test_missing_bounded_source_context_still_requires_context(self):
        source = ct.snapshot(context_complete=False)
        self.assertEqual(preview(source, **et.args(source))['status'], 'needs_context')

    def test_assistant_evidence_in_question_is_still_rejected(self):
        source = ct.snapshot('是的。', context=[ct.envelope('你是不是有糖尿病？', id='db:2', role='assistant')])
        ex = et.extraction(source, [], [et.question(source)])
        rv = json.loads(unclear_review(source, ex))
        rv['clarifications'][0]['evidence'].append({'source_ref': 'context_1', 'text': '你是不是有糖尿病？'})
        result = et.evaluate(source, ex, json.dumps(rv, ensure_ascii=False))
        self.assertEqual((result['status'], result['reason']), ('processing_failed', 'review_evidence_not_user'))

    def test_prompt_separates_all_user_evidence_from_read_context(self):
        source = ct.snapshot(); ex = et.extraction(source, [])
        prompt = prepare_review(source, ex, **et.args(source))['messages'][0]['content']
        self.assertIn('每一个evidence元素都只能引用role=user', prompt)
        self.assertIn('不能把助手问话放入clarifications.evidence', prompt)


def question_stage(h):
    source = h.source('医生说可能有高血压。')
    ex = et.extraction(source, [], [et.question(source)])
    result = pt.propose(h.state, source, permissions=et.args(source)['permissions'],
        request_key='first-question', extraction_response=ex, review_response=unclear_review(source, ex), **h.options())
    h.state = result['state']
    return source, result


def reply_arguments(h, original, body='说的是我本人，我可能有高血压。', *, empty=False):
    old_display = h.state['pending']['display_digest']
    source = h.source(body, context=original['messages'])
    items = [] if empty else [ct.disease(source['messages'][-1], value='高血压', certainty='possible')]
    ex = et.extraction(source, items); rv = et.review(source, ex)
    return dict(source=source, owner='user:1', permissions=et.args(source)['permissions'],
        request_key='reply-question', event=h.intent(old_display, 'resolve', reply=source['current_message_id']),
        extraction_response=ex, review_response=rv, **h.options())


class ClarificationDecisionTests(unittest.TestCase):
    def test_question_enters_pending_and_accept_cannot_save_it(self):
        h = pt.Harness(); _, result = question_stage(h)
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(len(h.state['pending']['questions']), 1)
        with self.assertRaisesRegex(NaturalMemoryContractError, 'clarification_required'): h.accept()
        self.assertEqual(h.eligible(), [])

    def test_first_fact_from_reply_gets_new_display_then_can_be_confirmed(self):
        h = pt.Harness(); original, _ = question_stage(h)
        old_display = h.state['pending']['display_digest']
        result = resolve(h.state, **reply_arguments(h, original))
        h.state = result['state']
        self.assertEqual(result['status'], 'needs_confirmation')
        self.assertNotEqual(result['display_digest'], old_display)
        self.assertEqual(h.eligible(), []); self.assertEqual(h.state['entries'], {})
        self.assertEqual(h.state['pending']['state_revision'], h.state['revision'])
        self.assertEqual(h.accept()['status'], 'projected_confirmed')
        entry = next(iter(h.state['entries'].values()))
        self.assertEqual(entry['fact']['content']['certainty'], 'possible')
        self.assertFalse(entry['clinical_confirmation'])

    def test_reply_about_family_can_close_question_without_self_fact(self):
        h = pt.Harness(); original, _ = question_stage(h)
        result = resolve(h.state, **reply_arguments(h, original, '说的是我父亲，不是我。', empty=True))
        self.assertEqual(result['status'], 'projected_resolved_without_change')
        self.assertIsNone(result['state']['pending']); self.assertEqual(result['state']['entries'], {})

    def test_reply_replay_does_not_advance_or_create_entries(self):
        h = pt.Harness(); original, _ = question_stage(h)
        arguments = reply_arguments(h, original)
        result = resolve(h.state, **arguments)
        again = resolve(result['state'], **arguments)
        self.assertEqual(again['status'], 'replayed')
        self.assertEqual(again['state'], result['state'])
        self.assertFalse(again['saved_ack'])

    def test_policy_change_invalidates_waiting_question(self):
        h = pt.Harness(); original, _ = question_stage(h)
        arguments = reply_arguments(h, original); h.policy['version'] += 1
        before = copy.deepcopy(h.state)
        with self.assertRaisesRegex(NaturalMemoryContractError, 'stale_plan'):
            resolve(h.state, **arguments)
        self.assertEqual(h.state, before)


@override_settings(ROOT_URLCONF='api.natural_memory_fixture_urls', NATURAL_MEMORY_ISOLATED_ENABLED=True,
                   NATURAL_MEMORY_ISOLATED_DATABASE='memory')
class ClarificationIntakeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='clarify-fictional-owner')
        self.conv = Conversation.objects.create(user=self.user, title='虚构澄清测试')
        intake.initialize_account(self.user)
        intake.configure(self.user, expected_epoch=0, expected_policy_version=1,
            collect_types=['self_disease_report', 'presentation_preference'], use_enabled=True,
            cloud_extraction=True, cloud_verification=True)
        self.client = APIClient(); self.client.force_authenticate(self.user); self.serial = 0

    def key(self):
        self.serial += 1; return 'clarification-' + str(self.serial)

    def row(self):
        return NaturalMemoryLedger.objects.get(user=self.user)

    def post(self, content, *, previous=None):
        row = self.row(); self.conv.refresh_from_db()
        body = {'request_key': self.key(), 'conversation_id': self.conv.pk, 'content': content,
            'context_ids': [previous['message_id']] if previous else [],
            'mode': 'clarification' if previous else 'new_message',
            'display_digest': row.state['pending']['display_digest'] if previous else None,
            'expected_revision': row.revision, 'expected_epoch': row.epoch,
            'expected_policy_version': row.policy['version'], 'expected_conversation_version': self.conv.memory_version}
        result = self.client.post('/natural-memory/messages/', body, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        return result.data

    def process(self, receipt, *, ask=False, empty=False):
        source = NaturalMemoryJob.objects.get(job_key=receipt['job_key']).snapshot
        items = [] if ask or empty else [ct.disease(source['messages'][-1], value='高血压', certainty='possible')]
        ex = et.extraction(source, items, [et.question(source)] if ask else [])
        self.assertEqual(intake.submit_reply(self.user, receipt['job_key'], stage='extraction', response=ex)['status'], 'awaiting_review')
        rv = unclear_review(source, ex) if ask else et.review(source, ex)
        return intake.submit_reply(self.user, receipt['job_key'], stage='review', response=rv)

    def wait(self):
        initial = self.post('医生说可能有高血压。')
        value = self.process(initial, ask=True)
        self.assertEqual(value['status'], 'needs_clarification'); self.assertFalse(value['saved_ack'])
        # Re-read the persisted state, rather than relying on an in-memory plan.
        self.assertEqual(len(self.row().state['pending']['questions']), 1)
        displayed = self.client.get('/natural-memory/state/').data['pending']['questions'][0]
        self.assertIsInstance(displayed['question'], str)
        return initial

    def test_wait_reply_display_confirm_persists_possible_and_source(self):
        initial = self.wait()
        reply = self.post('说的是我本人，我可能有高血压。', previous=initial)
        value = self.process(reply)
        self.assertEqual(value['status'], 'needs_confirmation'); self.assertFalse(value['saved_ack'])
        self.assertEqual(self.row().state['entries'], {})
        row = self.row()
        body = {'request_key': self.key(), 'conversation_id': self.conv.pk,
            'display_digest': row.state['pending']['display_digest'], 'decision': 'accept', 'reply_content': None,
            'expected_revision': row.revision, 'expected_epoch': row.epoch, 'expected_policy_version': row.policy['version']}
        result = self.client.post('/natural-memory/confirm/', body, format='json')
        self.assertEqual(result.status_code, 200, result.data); self.assertTrue(result.data['saved_ack'])
        entry = next(iter(self.row().state['entries'].values()))
        self.assertEqual(entry['fact']['content'], {'value_raw': '高血压', 'polarity': 'affirmed', 'certainty': 'possible'})
        self.assertFalse(entry['clinical_confirmation']); self.assertIsNone(self.row().state['pending'])

    def test_family_reply_does_not_persist_self_disease(self):
        initial = self.wait(); reply = self.post('说的是我父亲，不是我。', previous=initial)
        value = self.process(reply, empty=True)
        self.assertEqual(value['status'], 'projected_resolved_without_change')
        self.assertFalse(value['saved_ack']); self.assertEqual(self.row().state['entries'], {})
        self.assertIsNone(self.row().state['pending'])

    def test_another_unclear_reply_keeps_waiting_without_saving(self):
        initial = self.wait(); reply = self.post('我仍不清楚说的是谁。', previous=initial)
        self.assertEqual(self.process(reply, ask=True)['status'], 'needs_clarification')
        pending = self.row().state['pending']
        self.assertEqual(pending['source']['current_message_id'], 'db:' + str(reply['message_id']))
        self.assertEqual(self.row().state['entries'], {})

    def test_waiting_cannot_be_saved_by_clicking_accept(self):
        self.wait(); row = self.row()
        body = {'request_key': self.key(), 'conversation_id': self.conv.pk,
            'display_digest': row.state['pending']['display_digest'], 'decision': 'accept', 'reply_content': None,
            'expected_revision': row.revision, 'expected_epoch': row.epoch, 'expected_policy_version': row.policy['version']}
        result = self.client.post('/natural-memory/confirm/', body, format='json')
        self.assertNotEqual(result.status_code, 200)
        self.assertEqual(self.row().state['entries'], {})
