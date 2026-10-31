"""R5 authenticated view orchestration with explicit offline spies."""
import copy
import json
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import ChatMessage, Conversation
from .natural_memory_models import NaturalMemoryLedger, NaturalMemoryJob, NaturalMemoryOperation, NaturalMemoryAnswerGrant
from . import natural_memory_intake as intake
from . import natural_memory_answering as answering
from .test_natural_memory_extraction import extraction, review
from .test_natural_memory_contract import disease
from .test_natural_memory_retrieval import intent, answer, evidence


class OfflineProviders:
    offline_fixture = True

    def __init__(self, source_for_route, purpose='personal_application', scope='current'):
        self.source_for_route = source_for_route; self.purpose = purpose; self.scope = scope
        self.routing_inputs = []; self.answer_inputs = []; self.professional_inputs = []
        self.before_answer = None; self.after_answer = None; self.bad_answer = False

    def route(self, request):
        self.routing_inputs.append(copy.deepcopy(request))
        source = self.source_for_route()
        grants = answering.grants(User.objects.get(pk=int(source['owner'].split(':')[1])))
        return intent(source, self.purpose, scope=self.scope, grants=grants)

    def professional(self, question, purpose):
        self.professional_inputs.append((question, purpose)); return evidence()

    def answer(self, request):
        self.answer_inputs.append(copy.deepcopy(request))
        if self.before_answer:
            self.before_answer()
        result = 'bad JSON' if self.bad_answer else answer(request)
        if self.after_answer:
            self.after_answer()
        return result


@override_settings(ROOT_URLCONF='api.natural_memory_fixture_urls', NATURAL_MEMORY_ISOLATED_ENABLED=True,
    NATURAL_MEMORY_ISOLATED_DATABASE='memory')
class NaturalMemoryAnsweringTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='r5-fictional-user')
        self.other = User.objects.create_user(username='r5-fictional-other')
        self.conv = Conversation.objects.create(user=self.user)
        self.client = APIClient(); self.client.force_authenticate(self.user); self.serial = 0
        intake.initialize_account(self.user)
        intake.configure(self.user, expected_epoch=0, expected_policy_version=1,
            collect_types=['self_disease_report', 'presentation_preference'], use_enabled=True,
            cloud_extraction=True, cloud_verification=True)
        row = NaturalMemoryLedger.objects.get(user=self.user)
        answering.configure_grants(self.user, expected_version=0, expected_epoch=row.epoch,
            cloud_routing=True, cloud_answer_personal_context=True)

    def body(self, text='结合我之前说的情况，这份资料要关注什么？', conv=None, key=None):
        self.serial += 1; row = NaturalMemoryLedger.objects.get(user=self.user); conv = conv or self.conv
        conv.refresh_from_db()
        return {'request_key': key or 'r5-test-' + str(self.serial), 'conversation_id': conv.pk, 'content': text,
            'context_ids': [], 'mode': 'new_message', 'display_digest': None,
            'expected_revision': row.revision, 'expected_epoch': row.epoch,
            'expected_policy_version': row.policy['version'], 'expected_conversation_version': conv.memory_version}

    def source(self):
        job = NaturalMemoryJob.objects.filter(user=self.user, status='awaiting_extraction').latest('pk')
        return job.snapshot

    def save_health(self):
        response = self.client.post('/natural-memory/messages/', self.body('我可能有银屑病。'), format='json')
        self.assertEqual(response.status_code, 200, response.data)
        job = NaturalMemoryJob.objects.get(job_key=response.data['job_key']); source = job.snapshot
        ex = extraction(source, [disease(source['messages'][-1], certainty='possible')])
        intake.submit_reply(self.user, job.job_key, stage='extraction', response=ex)
        intake.submit_reply(self.user, job.job_key, stage='review', response=review(source, ex))
        state = intake.read_state(self.user); row = NaturalMemoryLedger.objects.get(user=self.user)
        body = {'request_key': 'r5-save', 'conversation_id': self.conv.pk,
            'display_digest': state['pending']['display_digest'], 'decision': 'accept', 'reply_content': None,
            'expected_revision': row.revision, 'expected_epoch': row.epoch, 'expected_policy_version': row.policy['version']}
        response = self.client.post('/natural-memory/confirm/', body, format='json'); self.assertEqual(response.status_code, 200, response.data)
        return response.data['revision']

    def ask(self, *, purpose='personal_application', text='结合我之前说的情况，这份资料要关注什么？', scope='current', providers=None, body=None):
        providers = providers or OfflineProviders(self.source, purpose, scope)
        conv = Conversation.objects.create(user=self.user)
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=providers):
            response = self.client.post('/natural-memory/questions/', body or self.body(text, conv), format='json')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data, providers

    def test_second_session_nameless_question_reaches_actual_answer_callback(self):
        self.save_health(); data, spy = self.ask()
        self.assertEqual(data['answer_status'], 'answered')
        payload = json.loads(spy.answer_inputs[0]['messages'][1]['content'])
        self.assertNotIn('银屑病', payload['question']); self.assertEqual(payload['personal_reports'][0]['name_raw'], '银屑病')
        self.assertEqual(payload['personal_reports'][0]['certainty'], 'possible')
        self.assertFalse(payload['personal_reports'][0]['clinical_confirmation'])
        self.assertNotIn('selection', spy.answer_inputs[0]); self.assertNotIn('intent', spy.answer_inputs[0])
        self.assertEqual(len(spy.professional_inputs), 1)
        self.assertNotIn('银屑病', json.dumps(spy.professional_inputs, ensure_ascii=False))

    def test_general_and_family_do_not_send_self_report(self):
        self.save_health()
        for purpose in ['general_knowledge', 'other_person']:
            data, spy = self.ask(purpose=purpose, text='解释这份资料。')
            self.assertEqual(data['answer_status'], 'answered')
            self.assertEqual(spy.answer_inputs[0]['payload']['personal_reports'], [])
            self.assertNotIn('银屑病', json.dumps(spy.answer_inputs[0]['messages'], ensure_ascii=False))

    def test_missing_personal_context_is_not_no_disease(self):
        data, spy = self.ask()
        self.assertEqual(data['answer_status'], 'answered')
        self.assertTrue(spy.answer_inputs[0]['payload']['limitations']['missing_personal_background_is_not_no_disease'])

    def test_personal_answer_permission_is_separate_from_collection(self):
        self.save_health(); row = NaturalMemoryLedger.objects.get(user=self.user)
        answering.configure_grants(self.user, expected_version=1, expected_epoch=row.epoch,
            cloud_routing=True, cloud_answer_personal_context=False)
        data, spy = self.ask()
        self.assertEqual(data['answer_status'], 'personal_answer_permission_required')
        self.assertFalse(spy.answer_inputs)
        self.assertTrue(NaturalMemoryLedger.objects.get(user=self.user).policy['collect_types'])

    def test_routing_off_prevents_callback_and_keeps_new_message(self):
        row = NaturalMemoryLedger.objects.get(user=self.user)
        answering.configure_grants(self.user, expected_version=1, expected_epoch=row.epoch,
            cloud_routing=False, cloud_answer_personal_context=False)
        data, spy = self.ask()
        self.assertEqual(data['answer_status'], 'answer_unavailable'); self.assertFalse(spy.routing_inputs)
        self.assertTrue(ChatMessage.objects.filter(pk=data['message_id']).exists())

    def test_repeated_HTTP_request_does_not_repeat_callbacks_or_messages(self):
        self.save_health(); body = self.body(); spy = OfflineProviders(self.source)
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=spy):
            one = self.client.post('/natural-memory/questions/', body, format='json')
            count = ChatMessage.objects.count()
            two = self.client.post('/natural-memory/questions/', body, format='json')
        self.assertEqual(one.data['answer_status'], 'answered'); self.assertEqual(two.data['status'], 'replayed')
        self.assertEqual(len(spy.answer_inputs), 1); self.assertEqual(ChatMessage.objects.count(), count)

    def test_answer_failure_preserves_received_message_and_no_assistant_half_write(self):
        self.save_health(); spy = OfflineProviders(self.source); spy.bad_answer = True
        before = ChatMessage.objects.filter(role='assistant').count(); data, _ = self.ask(providers=spy)
        self.assertEqual(data['answer_status'], 'answer_unavailable')
        self.assertTrue(ChatMessage.objects.filter(pk=data['message_id'], role='user').exists())
        self.assertEqual(ChatMessage.objects.filter(role='assistant').count(), before)

    def test_provider_exception_is_sanitized_without_retry(self):
        self.save_health(); spy = OfflineProviders(self.source)
        def fail():
            raise RuntimeError('fake-key-do-not-leak')
        spy.before_answer = fail
        data, _ = self.ask(providers=spy)
        self.assertEqual(data['answer_status'], 'answer_failed'); self.assertNotIn('fake-key', json.dumps(data))
        self.assertEqual(len(spy.answer_inputs), 1)

    def test_inflight_policy_change_prevents_storing_or_displaying_old_answer(self):
        self.save_health(); spy = OfflineProviders(self.source)
        def revoke():
            row = NaturalMemoryLedger.objects.get(user=self.user)
            answering.configure_grants(self.user, expected_version=1, expected_epoch=row.epoch,
                cloud_routing=True, cloud_answer_personal_context=False)
        spy.after_answer = revoke
        data, _ = self.ask(providers=spy)
        self.assertEqual(data['answer_status'], 'answer_unavailable')
        self.assertEqual(data['answer_result']['reason'], 'stale_answer_context')
        self.assertIsNone(data['answer_result']['answer'])
        self.assertFalse(ChatMessage.objects.filter(role='assistant').exists())

    def test_inflight_source_edit_without_version_change_rechecks_exact_payload(self):
        self.save_health(); old = ChatMessage.objects.filter(role='user').first(); spy = OfflineProviders(self.source)
        spy.after_answer = lambda: ChatMessage.objects.filter(pk=old.pk).update(content='说的是我父亲。')
        data, _ = self.ask(providers=spy)
        self.assertEqual(data['answer_status'], 'answer_unavailable')
        self.assertEqual(data['answer_result']['reason'], 'stale_answer_input')
        self.assertFalse(ChatMessage.objects.filter(role='assistant').exists())

    def test_audit_records_selected_excluded_and_input_hash_not_prompt_copies(self):
        self.save_health(); data, spy = self.ask()
        ticket = NaturalMemoryOperation.objects.get(user=self.user, request_key='r5-answer-' + str(data['message_id'])).receipt
        self.assertTrue(ticket['model_input_hash']); self.assertEqual(ticket['routing_calls'], 1); self.assertEqual(ticket['answer_calls'], 1)
        self.assertEqual(len(ticket['selected']), 1); self.assertNotIn('银屑病', json.dumps(ticket, ensure_ascii=False))
        from .natural_memory_contract import digest
        self.assertEqual(ticket['model_input_hash'], digest(spy.answer_inputs[0]['messages']))

    def test_unconfigured_providers_do_not_silently_use_real_models(self):
        data, _ = self.ask(providers=OfflineProviders(self.source))
        # Explicit local fixture path only; an ordinary route without injected
        # providers persists its question and reports not configured.
        response = self.client.post('/natural-memory/questions/', self.body(), format='json')
        self.assertEqual(response.data['answer_status'], 'not_configured')

    def test_public_payload_cannot_choose_intent_or_inject_evidence(self):
        body = self.body(); body['purpose'] = 'personal_application'
        response = self.client.post('/natural-memory/questions/', body, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ChatMessage.objects.exists())

    def test_no_current_account_grant_reads_do_not_create_one(self):
        self.assertIsNone(answering.grants(self.other))
        self.assertEqual(NaturalMemoryAnswerGrant.objects.count(), 1)

    def test_production_URL_not_mounted(self):
        from django.urls import resolve, Resolver404
        with self.assertRaises(Resolver404):
            resolve('/natural-memory/questions/', urlconf='knowledge_graph_backend.urls')

    def test_expired_original_ticket_cannot_restart_a_model_attempt(self):
        self.save_health(); spy = OfflineProviders(self.source)
        original = answering._step
        def expire(user, key, phase, source):
            from django.utils import timezone
            from datetime import timedelta
            op = NaturalMemoryOperation.objects.get(user=user, request_key=key)
            data = op.receipt; data['deadline_at'] = (timezone.now() - timedelta(seconds=1)).isoformat()
            op.receipt = data; op.save(update_fields=['receipt'])
            return original(user, key, phase, source)
        with patch.object(answering, '_step', side_effect=expire):
            data, _ = self.ask(providers=spy)
        self.assertEqual(data['answer_status'], 'answer_unavailable')
        self.assertFalse(spy.routing_inputs); self.assertFalse(spy.answer_inputs)

    def test_cross_account_question_id_does_not_reveal_or_generate(self):
        self.save_health(); foreign_conv = Conversation.objects.create(user=self.other)
        message = ChatMessage.objects.create(conversation=foreign_conv, role='user', content='虚构他人问题')
        with self.assertRaisesMessage(Exception, 'question_unavailable'):
            answering.run_answer(self.user, message.pk, context_ids=[], providers=OfflineProviders(self.source))
        self.assertFalse(ChatMessage.objects.filter(conversation=foreign_conv, role='assistant').exists())

    def test_missing_professional_evidence_does_not_use_memory_as_medical_source(self):
        self.save_health(); spy = OfflineProviders(self.source); spy.professional = lambda question, purpose: []
        data, _ = self.ask(providers=spy)
        self.assertEqual(data['answer_status'], 'no_professional_evidence'); self.assertFalse(spy.answer_inputs)

    def test_delayed_first_answer_attempt_cannot_get_new_ninety_seconds(self):
        from datetime import timedelta
        from django.utils import timezone
        response = self.client.post('/natural-memory/messages/', self.body(), format='json')
        mid = response.data['message_id']
        ChatMessage.objects.filter(pk=mid).update(created_at=timezone.now() - timedelta(seconds=91))
        spy = OfflineProviders(self.source)
        result = answering.run_answer(self.user, mid, context_ids=[], providers=spy)
        self.assertEqual(result['reason'], 'answer_deadline')
        self.assertFalse(spy.routing_inputs); self.assertFalse(spy.answer_inputs)

    def test_saved_preference_is_audited_as_personal_use_separate_from_health(self):
        from .test_natural_memory_policy import preference
        posted = self.client.post('/natural-memory/messages/', self.body('以后简短一些。'), format='json')
        job = NaturalMemoryJob.objects.get(job_key=posted.data['job_key'])
        intake.submit_reply(self.user, job.job_key, stage='extraction',
            response=extraction(job.snapshot, [preference(job.snapshot)]))
        state = intake.read_state(self.user); row = NaturalMemoryLedger.objects.get(user=self.user)
        confirmed = self.client.post('/natural-memory/confirm/', {'request_key': 'r5-pref-save',
            'conversation_id': self.conv.pk, 'display_digest': state['pending']['display_digest'], 'decision': 'accept',
            'reply_content': None, 'expected_revision': row.revision, 'expected_epoch': row.epoch,
            'expected_policy_version': row.policy['version']}, format='json')
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        data, spy = self.ask(purpose='general_knowledge', text='介绍这份资料。')
        self.assertTrue(data['answer_result']['personal_context_used'])
        self.assertTrue(data['answer_result']['saved_preferences_used']); self.assertFalse(data['answer_result']['health_context_used'])
        ticket = NaturalMemoryOperation.objects.get(user=self.user, request_key='r5-answer-' + str(data['message_id'])).receipt
        self.assertEqual(ticket['presentation_preferences'][0]['origin'], 'saved')
        self.assertLessEqual(spy.answer_inputs[0]['constraints']['timeout_seconds_cap'], 45)
        self.assertLessEqual(spy.answer_inputs[0]['constraints']['timeout_seconds_cap'], spy.answer_inputs[0]['remaining_original_seconds'])
