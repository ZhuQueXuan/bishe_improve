"""Synthetic UI adapter checks. Not browser/medical/language acceptance."""
import json
from unittest.mock import patch
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from .models import Conversation, ChatMessage
from .natural_memory_models import NaturalMemoryLedger, NaturalMemoryJob, NaturalMemoryAnswerGrant
from . import natural_memory_intake as intake
from . import natural_memory_panel as panel
from . import test_natural_memory_answering as answering_tests
from .natural_memory_contract import CANDIDATE_CONTRACT
from .natural_memory_extraction import EXTRACTION_CONTRACT, REVIEW_CONTRACT


class ScriptedPanelWorker:
    """Exact frozen synthetic examples only; deliberately fails unfamiliar text."""
    offline_fixture = True
    def __init__(self):
        self.extract_calls = 0; self.review_calls = 0

    def extract(self, request):
        self.extract_calls += 1
        payload = json.loads(request['messages'][1]['content'])
        current = next(s for s in payload['sources'] if s['source_ref'] == 'current')['content']
        cases = {'我可能有银屑病。': ('银屑病', 'possible', None),
            '更正一下，之前那条应是湿疹。': ('湿疹', 'asserted', None),
            '我去年得过湿疹。': ('湿疹', 'asserted', '去年')}
        if current in cases:
            name, certainty, when = cases[current]
            candidate = {'contract': CANDIDATE_CONTRACT, 'category': 'self_disease_report', 'subject': 'self',
                'content': {'value_raw': name, 'polarity': 'affirmed', 'certainty': certainty},
                'time': {'raw': when, 'temporal_scope': 'past' if when else 'unspecified'},
                'relation': 'new_information', 'evidence': [{'source_ref': 'current', 'text': current}]}
            items = [candidate]
        elif current == '以后回答简短一些。':
            items = [{'contract': CANDIDATE_CONTRACT, 'category': 'presentation_preference', 'subject': 'self',
                'content': {'slot': 'length', 'value': 'brief', 'scope': 'long_term'},
                'time': {'raw': None, 'temporal_scope': 'not_applicable'}, 'relation': 'new_information',
                'evidence': [{'source_ref': 'current', 'text': current}]}]
        elif current == '那是我父亲的情况。':
            items = []
        else:
            raise RuntimeError('unrecognized_synthetic_example')
        return json.dumps({'contract': EXTRACTION_CONTRACT, 'request_ref': request['request_ref'],
            'outcome': 'propose' if items else 'no_memory', 'items': items, 'clarifications': []}, ensure_ascii=False)

    def review(self, request):
        self.review_calls += 1
        payload = json.loads(request['messages'][1]['content'])
        current = next(s for s in payload['sources'] if s['source_ref'] == 'current')['content']
        return json.dumps({'contract': REVIEW_CONTRACT, 'request_ref': request['request_ref'],
            'items': [{'item_ref': item['item_ref'], 'verdict': 'supported', 'reason': 'meaning_supported',
                'evidence': item['candidate']['evidence']} for item in payload['items']], 'clarifications': [],
            'coverage': {'verdict': 'complete', 'reason': 'coverage_complete',
                'examined_refs': [s['source_ref'] for s in payload['sources']],
                'evidence': [{'source_ref': 'current', 'text': current}]}}, ensure_ascii=False)


@override_settings(ROOT_URLCONF='api.natural_memory_fixture_urls', NATURAL_MEMORY_ISOLATED_ENABLED=True,
    NATURAL_MEMORY_ISOLATED_DATABASE='memory', NATURAL_MEMORY_PANEL_ENABLED=True)
class NaturalMemoryPanelTests(TestCase):
    setUp = answering_tests.NaturalMemoryAnsweringTests.setUp
    body = answering_tests.NaturalMemoryAnsweringTests.body
    source = answering_tests.NaturalMemoryAnsweringTests.source
    save_health = answering_tests.NaturalMemoryAnsweringTests.save_health

    def state(self):
        response = self.client.get('/natural-memory/panel/')
        self.assertEqual(response.status_code, 200, response.data); return response.data

    def settings_body(self, **changes):
        state = self.state(); self.serial += 1
        return {'request_key': 'r7-settings-' + str(self.serial), 'expected_revision': state['revision'],
            'expected_epoch': state['epoch'], 'expected_policy_version': state['policy_version'],
            'expected_answer_grant_version': state['answer_grant_version'], **state['settings'], **changes}

    def send(self, content='我可能有银屑病。', relation=None, worker=None):
        with override_settings(NATURAL_MEMORY_PANEL_WORKER=worker or ScriptedPanelWorker()):
            result = self.client.post('/natural-memory/panel/messages/',
                {'message': self.body(content), 'relation': relation}, format='json')
        self.assertEqual(result.status_code, 200, result.data); return result.data

    def confirm(self):
        state = self.state(); self.serial += 1
        data = {'request_key': 'r7-confirm-' + str(self.serial), 'conversation_id': self.conv.pk,
            'expected_revision': state['revision'], 'expected_epoch': state['epoch'],
            'expected_policy_version': state['policy_version'], 'display_digest': state['pending']['display_digest'],
            'decision': 'accept', 'reply_content': None}
        result = self.client.post('/natural-memory/confirm/', data, format='json')
        self.assertEqual(result.status_code, 200, result.data); return result.data

    def test_read_is_authenticated_and_does_not_write(self):
        before = NaturalMemoryLedger.objects.get(user=self.user).lock_version
        state = self.state(); self.assertFalse(state['writes_performed'])
        self.assertEqual(NaturalMemoryLedger.objects.get(user=self.user).lock_version, before)
        self.assertNotIn('token', json.dumps(state)); self.assertEqual(APIClient().get('/natural-memory/panel/').status_code, 401)

    def test_disabled_panel_and_production_url_are_closed(self):
        with override_settings(NATURAL_MEMORY_PANEL_ENABLED=False):
            self.assertEqual(self.client.get('/natural-memory/panel/').status_code, 404)
        with override_settings(ROOT_URLCONF='api.urls'):
            self.assertEqual(self.client.get('/natural-memory/panel/').status_code, 404)

    def test_new_account_settings_are_all_off_and_not_inherited(self):
        intake.initialize_account(self.other)
        self.client.force_authenticate(self.other)
        state = self.state(); self.assertFalse(any(state['settings'].values())); self.assertFalse(state['entries'])

    def test_settings_independent_flags_and_gate_not_opened(self):
        body = self.settings_body(collect_health=False, cloud_verification=False, cloud_routing=False,
            preference_auto_enabled=True)
        result = self.client.post('/natural-memory/panel/settings/', body, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        state = self.state()
        self.assertEqual(state['settings'], {k: body[k] for k in panel.FLAGS})
        self.assertFalse(state['preference_auto_gate_passed'])

    def test_client_cannot_supply_release_gate(self):
        body = self.settings_body(preference_auto_gate_passed=True)
        self.assertEqual(self.client.post('/natural-memory/panel/settings/', body, format='json').status_code, 400)

    def test_stale_settings_rejected_and_same_key_replay_is_metadata(self):
        body = self.settings_body(cloud_routing=False)
        self.assertEqual(self.client.post('/natural-memory/panel/settings/', body, format='json').status_code, 200)
        again = self.client.post('/natural-memory/panel/settings/', body, format='json')
        self.assertEqual(again.data['status'], 'replayed')
        body['request_key'] += '-new'
        self.assertEqual(self.client.post('/natural-memory/panel/settings/', body, format='json').status_code, 409)

    def test_settings_second_half_failure_rolls_back_all_flags(self):
        before = self.state()
        with patch.object(panel.answering, 'configure_grants', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                panel.update_settings(self.user, self.settings_body(use_enabled=False))
        self.assertEqual(self.state(), before)

    def test_health_is_pending_not_saved_then_confirmed(self):
        result = self.send(); self.assertFalse(result['saved_ack']); self.assertEqual(result['processing_status'], 'needs_confirmation')
        state = self.state(); self.assertFalse(state['entries']); self.assertIsNotNone(state['pending'])
        self.assertEqual(state['pending_session'], 'conversation:' + str(self.conv.pk))
        self.assertEqual(self.confirm()['saved_ack'], True)
        entry = self.state()['entries'][0]
        self.assertEqual(entry['fact']['content']['certainty'], 'possible'); self.assertTrue(entry['user_confirmed'])

    def test_preference_no_review_and_no_gate_still_confirm(self):
        worker = ScriptedPanelWorker()
        result = self.send('以后回答简短一些。', worker=worker)
        self.assertEqual(result['processing_status'], 'needs_confirmation'); self.assertEqual(worker.review_calls, 0)

    def test_unknown_example_is_failed_not_empty_or_saved(self):
        result = self.send('这个样例没有离线回复。')
        self.assertEqual(result['processing_status'], 'processing_failed'); self.assertFalse(result['saved_ack'])
        self.assertTrue(ChatMessage.objects.filter(pk=result['message_id']).exists())
        job = NaturalMemoryJob.objects.get(job_key=result['job_key'])
        self.assertEqual(job.status, 'failed'); self.assertEqual(job.snapshot, {}); self.assertFalse(self.state()['pending'])

    def test_worker_not_configured_stores_message_without_success_claim(self):
        with override_settings(NATURAL_MEMORY_PANEL_WORKER=None):
            result = panel.process_message(self.user, {'message': self.body(), 'relation': None})
        self.assertEqual(result['processing_status'], 'worker_not_configured'); self.assertFalse(result['saved_ack'])

    def test_collection_off_makes_no_worker_call(self):
        self.client.post('/natural-memory/panel/settings/', self.settings_body(collect_health=False, collect_preferences=False), format='json')
        worker = ScriptedPanelWorker(); result = self.send(worker=worker)
        self.assertEqual(result['status'], 'stored_without_collection'); self.assertEqual(worker.extract_calls, 0)

    def test_explicit_correction_target_then_confirm_replaces_fact(self):
        self.save_health(); entry = self.state()['entries'][0]
        relation = {'operation': 'correction', 'target': entry['key'], 'target_version': entry['version']}
        result = self.send('更正一下，之前那条应是湿疹。', relation)
        self.assertEqual(result['processing_status'], 'needs_confirmation')
        state = self.state(); self.assertEqual(state['entries'][0]['status'], 'disputed')
        self.assertEqual(state['pending']['actions'][0]['previous_fact']['content']['value_raw'], '银屑病')
        self.confirm(); self.assertEqual(self.state()['entries'][0]['fact']['content']['value_raw'], '湿疹')

    def test_stale_target_and_foreign_conversation_reject_before_message(self):
        self.save_health(); entry = self.state()['entries'][0]; before = ChatMessage.objects.count()
        body = {'message': self.body(), 'relation': {'operation': 'correction', 'target': entry['key'], 'target_version': entry['version'] + 1}}
        self.assertEqual(self.client.post('/natural-memory/panel/messages/', body, format='json').status_code, 409)
        conv = Conversation.objects.create(user=self.other)
        body = {'message': self.body(conv=conv), 'relation': None}
        self.assertEqual(self.client.post('/natural-memory/panel/messages/', body, format='json').status_code, 400)
        self.assertEqual(ChatMessage.objects.count(), before)

    def test_raw_candidate_and_model_reply_cannot_be_submitted(self):
        body = {'message': self.body(), 'relation': None, 'candidate': {}, 'model_reply': 'fake'}
        self.assertEqual(self.client.post('/natural-memory/panel/messages/', body, format='json').status_code, 400)

    def test_new_conversation_has_no_history_enumeration_and_replays_once(self):
        body = {k: v for k, v in self.settings_body().items() if k in {'request_key','expected_revision','expected_epoch','expected_policy_version'}}
        first = self.client.post('/natural-memory/panel/conversations/', body, format='json')
        self.assertEqual(first.status_code, 200); self.assertEqual(len(self.state()['conversations']), 2)
        self.client.post('/natural-memory/panel/conversations/', body, format='json')
        self.assertEqual(len(self.state()['conversations']), 2); self.assertFalse(ChatMessage.objects.exists())

    def test_settings_change_invalidates_paused_checkpoint(self):
        spy = answering_tests.OfflineProviders(self.source)
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=spy):
            paused = self.client.post('/natural-memory/questions/pause/', self.body(), format='json').data['answer_result']
            self.assertEqual(paused['status'], 'answer_paused')
            self.client.post('/natural-memory/panel/settings/', self.settings_body(cloud_answer_personal_context=False), format='json')
            state = self.state()
            result = self.client.post('/natural-memory/resume/', {'checkpoint_key': paused['checkpoint_key'],
                'expected_revision': state['revision'], 'expected_epoch': state['epoch']}, format='json')
        self.assertIsNone(result.data.get('answer')); self.assertEqual(len(spy.answer_inputs), 0)

    def test_wrong_bool_type_and_unconfigured_account_do_not_initialize(self):
        self.assertEqual(self.client.post('/natural-memory/panel/settings/', self.settings_body(use_enabled=1), format='json').status_code, 400)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get('/natural-memory/panel/').status_code, 409)
        self.assertFalse(NaturalMemoryAnswerGrant.objects.filter(user=self.other).exists())

    def test_answer_exposes_referenced_basis_with_uncertainty(self):
        self.save_health(); spy = answering_tests.OfflineProviders(self.source)
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=spy):
            result = self.client.post('/natural-memory/questions/', self.body(), format='json').data['answer_result']
        self.assertEqual(result['basis']['personal_reports'][0]['certainty'], 'possible')
        self.assertFalse(result['basis']['personal_reports'][0]['clinical_confirmation'])
        self.assertEqual(result['basis']['professional_evidence'][0]['source']['page'], 1)
        self.assertNotIn('db:', json.dumps(result['basis']))

    def test_general_answer_basis_does_not_expose_saved_health(self):
        self.save_health(); spy = answering_tests.OfflineProviders(self.source, purpose='general_knowledge')
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=spy):
            result = self.client.post('/natural-memory/questions/', self.body(), format='json').data['answer_result']
        self.assertFalse(result['basis']['personal_reports'])
        self.assertNotIn('银屑病', json.dumps(result['basis'], ensure_ascii=False))

    def test_replayed_correction_does_not_reprocess_new_target_version(self):
        self.save_health(); entry = self.state()['entries'][0]; worker = ScriptedPanelWorker()
        body = {'message': self.body('更正一下，之前那条应是湿疹。'),
            'relation': {'operation': 'correction', 'target': entry['key'], 'target_version': entry['version']}}
        with override_settings(NATURAL_MEMORY_PANEL_WORKER=worker):
            one = self.client.post('/natural-memory/panel/messages/', body, format='json')
            two = self.client.post('/natural-memory/panel/messages/', body, format='json')
        self.assertEqual(one.data['processing_status'], 'needs_confirmation')
        self.assertEqual(two.data['status'], 'replayed'); self.assertFalse(two.data['saved_ack'])
        self.assertEqual(worker.extract_calls, 1); self.assertEqual(worker.review_calls, 1)

    def test_trusted_synthetic_auto_gate_allows_preference_and_undo(self):
        row = NaturalMemoryLedger.objects.get(user=self.user)
        intake.configure(self.user, expected_epoch=row.epoch, expected_policy_version=row.policy['version'],
            collect_types=row.policy['collect_types'], use_enabled=True, cloud_extraction=True,
            cloud_verification=True, preference_auto_enabled=True, preference_auto_gate_passed=True)
        result = self.send('以后回答简短一些。')
        self.assertEqual(result['processing_status'], 'projected_auto_preference'); self.assertTrue(result['saved_ack'])
        state = self.state(); entry = state['entries'][0]
        self.assertFalse(entry['user_confirmed']); self.assertIsNone(state['pending'])
        body = {'request_key': 'r7-auto-undo', 'conversation_id': self.conv.pk, 'target': entry['key'],
            'target_version': entry['version'], 'display_digest': entry['display_digest'], 'action': 'retraction',
            'expected_revision': state['revision'], 'expected_epoch': state['epoch'], 'expected_policy_version': state['policy_version']}
        self.assertEqual(self.client.post('/natural-memory/manage/', body, format='json').status_code, 200)
        self.assertEqual(self.state()['entries'][0]['status'], 'retracted')
