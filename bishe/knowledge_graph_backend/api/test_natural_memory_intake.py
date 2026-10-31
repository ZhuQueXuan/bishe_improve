"""R4 HTTP + transactional tests with fictional users/messages and offline replies."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.utils import timezone
from datetime import timedelta
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework.authtoken.models import Token

from .models import ChatMessage, Conversation, MemorySourceExclusion
from .natural_memory_models import NaturalMemoryLedger, NaturalMemoryJob, NaturalMemoryOperation
from .natural_memory_contract import NaturalMemoryContractError, digest
from . import natural_memory_intake as intake
from .test_natural_memory_contract import disease
from .test_natural_memory_extraction import args, extraction, review, question
from .test_natural_memory_policy import preference


@override_settings(ROOT_URLCONF='api.natural_memory_fixture_urls', NATURAL_MEMORY_ISOLATED_ENABLED=True,
    NATURAL_MEMORY_ISOLATED_DATABASE='memory')
class NaturalMemoryIntakeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='r4-fictional-owner')
        self.other = User.objects.create_user(username='r4-fictional-other')
        self.conv = Conversation.objects.create(user=self.user, title='虚构R4会话')
        self.other_conv = Conversation.objects.create(user=self.other)
        self.client = APIClient(); self.client.force_authenticate(self.user)
        self.serial = 0
        intake.initialize_account(self.user)
        intake.configure(self.user, expected_epoch=0, expected_policy_version=1,
            collect_types=['self_disease_report', 'presentation_preference'], use_enabled=True,
            cloud_extraction=True, cloud_verification=True)

    def key(self):
        self.serial += 1; return 'http-' + str(self.serial)

    def versions(self):
        row = NaturalMemoryLedger.objects.get(user=self.user)
        return {'expected_revision': row.revision, 'expected_epoch': row.epoch,
            'expected_policy_version': row.policy['version']}

    def message_body(self, content='我患有银屑病。', **changes):
        self.conv.refresh_from_db()
        return {'request_key': self.key(), 'conversation_id': self.conv.pk, 'content': content,
            'context_ids': [], 'mode': 'new_message', 'display_digest': None,
            'expected_conversation_version': self.conv.memory_version, **self.versions(), **changes}

    def post(self, content='我患有银屑病。', **changes):
        response = self.client.post('/natural-memory/messages/', self.message_body(content, **changes), format='json')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def state(self):
        response = self.client.get('/natural-memory/state/')
        self.assertEqual(response.status_code, 200, response.data); return response.data

    def process(self, receipt, *, items=None, questions=None, verdict='supported', choices=None):
        job = NaturalMemoryJob.objects.get(job_key=receipt['job_key']); source = job.snapshot
        ex = extraction(source, items=items, questions=questions)
        result = intake.submit_reply(self.user, job.job_key, stage='extraction', response=ex)
        if result['status'] == 'awaiting_review':
            result = intake.submit_reply(self.user, job.job_key, stage='review',
                response=review(source, ex, verdict=verdict), choices=choices)
        return result

    def confirm_body(self, **changes):
        state = self.state()
        return {'request_key': self.key(), 'conversation_id': self.conv.pk,
            'display_digest': state['pending']['display_digest'], 'decision': 'accept', 'reply_content': None,
            **self.versions(), **changes}

    def accept(self, **changes):
        result = self.client.post('/natural-memory/confirm/', self.confirm_body(**changes), format='json')
        self.assertEqual(result.status_code, 200, result.data); return result.data

    def save_health(self):
        receipt = self.post(); self.process(receipt); self.accept()
        return self.state()['entries'][0]

    def manage_body(self, entry, action='forget', **changes):
        return {'request_key': self.key(), 'conversation_id': self.conv.pk, 'target': entry['key'],
            'target_version': entry['version'], 'display_digest': entry['display_digest'], 'action': action,
            **self.versions(), **changes}

    def test_message_http_creates_owned_source_and_bounded_job(self):
        receipt = self.post(); job = NaturalMemoryJob.objects.get(job_key=receipt['job_key'])
        message = ChatMessage.objects.get(pk=receipt['message_id'])
        self.assertEqual(message.conversation.user_id, self.user.pk)
        self.assertEqual(job.snapshot['messages'][-1]['content'], message.content)
        request = intake.worker_request(self.user, job.job_key)
        self.assertFalse(request['transport_implemented']); self.assertEqual(request['constraints']['sdk_retries'], 0)
        self.assertFalse(receipt['saved_ack']); self.assertEqual(self.state()['entries'], [])

    def test_health_preview_then_click_saves_with_real_source(self):
        receipt = self.post(); self.process(receipt)
        preview = self.state(); self.assertIsNotNone(preview['pending']); self.assertEqual(preview['entries'], [])
        saved = self.accept(); entry = self.state()['entries'][0]
        self.assertTrue(saved['saved_ack']); self.assertTrue(entry['user_confirmed']); self.assertTrue(entry['eligible'])
        self.assertFalse(entry['clinical_confirmation']); self.assertEqual(entry['fact']['content']['certainty'], 'asserted')
        # This helper's scripted candidate is asserted; no language inference is being tested.
        self.assertEqual(entry['sources'][0]['message_id'], 'db:' + str(receipt['message_id']))

    def test_scripted_possible_value_and_time_preserved(self):
        receipt = self.post('去年医生说我可能有银屑病。'); source = NaturalMemoryJob.objects.get(job_key=receipt['job_key']).snapshot
        c = disease(source['messages'][-1], certainty='possible'); c['time'] = {'raw': '去年', 'temporal_scope': 'past'}
        self.process(receipt, items=[c]); self.accept(reply_content='确认保存这条怀疑。')
        entry = self.state()['entries'][0]
        self.assertEqual(entry['fact']['content']['certainty'], 'possible'); self.assertEqual(entry['fact']['time']['raw'], '去年')
        self.assertFalse(entry['clinical_confirmation'])

    def test_token_authentication_and_anonymous_block(self):
        client = APIClient(); result = client.post('/natural-memory/messages/', self.message_body(), format='json')
        self.assertIn(result.status_code, [401, 403]); self.assertEqual(ChatMessage.objects.count(), 0)
        token = Token.objects.create(user=self.user); client.credentials(HTTP_AUTHORIZATION='Token ' + token.key)
        result = client.post('/natural-memory/messages/', self.message_body(), format='json')
        self.assertEqual(result.status_code, 200)

    def test_foreign_conversation_and_context_rejected_without_message(self):
        result = self.client.post('/natural-memory/messages/', self.message_body(conversation_id=self.other_conv.pk), format='json')
        self.assertEqual(result.status_code, 400); self.assertEqual(ChatMessage.objects.count(), 0)
        foreign = ChatMessage.objects.create(conversation=self.other_conv, role='user', content='虚构他人信息')
        result = self.client.post('/natural-memory/messages/', self.message_body(context_ids=[foreign.pk]), format='json')
        self.assertEqual(result.status_code, 400); self.assertEqual(ChatMessage.objects.filter(conversation=self.conv).count(), 0)

    def test_untrusted_candidate_owner_or_policy_fields_never_accepted(self):
        for field, value in [('candidate', {}), ('owner', 'user:999'), ('policy', {}), ('review_response', '{}')]:
            result = self.client.post('/natural-memory/messages/', self.message_body(**{field: value}), format='json')
            self.assertEqual(result.status_code, 400); self.assertEqual(result.data['reason'], 'message_input_fields')
        self.assertEqual(ChatMessage.objects.count(), 0)

    def test_payload_and_context_budget_fail_atomically(self):
        result = self.client.post('/natural-memory/messages/', self.message_body('中' * 1400), format='json')
        self.assertEqual(result.status_code, 400)
        result = self.client.post('/natural-memory/messages/', self.message_body(context_ids=[1, 2, 3, 4, 5]), format='json')
        self.assertEqual(result.status_code, 400); self.assertEqual(ChatMessage.objects.count(), 0)

    def test_new_message_idempotence_does_not_duplicate_message_job_or_receipt(self):
        body = self.message_body(); one = self.client.post('/natural-memory/messages/', body, format='json')
        two = self.client.post('/natural-memory/messages/', body, format='json')
        self.assertEqual(one.status_code, 200); self.assertEqual(two.data['status'], 'replayed')
        self.assertEqual(ChatMessage.objects.count(), 1); self.assertEqual(NaturalMemoryJob.objects.count(), 1)
        self.assertEqual(NaturalMemoryOperation.objects.count(), 1); self.assertFalse(two.data['saved_ack'])

    def test_key_reuse_different_input_and_stale_window_rejected(self):
        body = self.message_body(); self.client.post('/natural-memory/messages/', body, format='json')
        reused = self.client.post('/natural-memory/messages/', {**body, 'content': '不同内容'}, format='json')
        stale = self.client.post('/natural-memory/messages/', {**body, 'request_key': 'other-window'}, format='json')
        self.assertEqual(reused.status_code, 409); self.assertEqual(stale.status_code, 409)
        self.assertEqual(ChatMessage.objects.count(), 1)

    def test_extraction_reply_requires_review_and_closes_exactly_once(self):
        receipt = self.post(); job = NaturalMemoryJob.objects.get(job_key=receipt['job_key']); ex = extraction(job.snapshot)
        one = intake.submit_reply(self.user, job.job_key, stage='extraction', response=ex)
        two = intake.submit_reply(self.user, job.job_key, stage='extraction', response=ex)
        self.assertEqual(one['status'], 'awaiting_review'); self.assertEqual(two['status'], 'replayed')
        self.assertIsNone(self.state()['pending']); self.assertEqual(self.state()['entries'], [])
        request = intake.worker_request(self.user, job.job_key); self.assertEqual(request['stage'], 'review')

    def test_bad_reply_records_failure_without_save_or_raw_persistence(self):
        receipt = self.post(); result = intake.submit_reply(self.user, receipt['job_key'], stage='extraction', response='not json')
        self.assertEqual(result['status'], 'processing_failed'); self.assertFalse(result['saved_ack'])
        job = NaturalMemoryJob.objects.get(job_key=receipt['job_key'])
        self.assertEqual(job.status, 'failed'); self.assertEqual(job.snapshot, {}); self.assertEqual(job.extraction_reply, '')
        self.assertIsNone(self.state()['pending']); self.assertEqual(self.state()['entries'], [])

    def test_new_message_closes_old_task_and_hides_old_display(self):
        old = self.post(); job = NaturalMemoryJob.objects.get(job_key=old['job_key']); ex = extraction(job.snapshot)
        self.post('我想换个问题。')
        with self.assertRaisesMessage(NaturalMemoryContractError, 'job_closed'):
            intake.submit_reply(self.user, job.job_key, stage='extraction', response=ex)
        job.refresh_from_db(); self.assertEqual(job.status, 'stale'); self.assertEqual(job.snapshot, {})
        self.assertIsNone(self.state()['pending'])

    def test_new_message_blocks_prior_confirmation_even_in_other_conversation(self):
        self.process(self.post()); body = self.confirm_body()
        conv = Conversation.objects.create(user=self.user)
        self.post('新会话问题。', conversation_id=conv.pk)
        result = self.client.post('/natural-memory/confirm/', body, format='json')
        self.assertEqual(result.status_code, 409); self.assertFalse(result.data['saved_ack'])

    def test_policy_change_closes_job_and_pending(self):
        receipt = self.post(); row = NaturalMemoryLedger.objects.get(user=self.user)
        intake.configure(self.user, expected_epoch=row.epoch, expected_policy_version=row.policy['version'],
            collect_types=[], use_enabled=False, cloud_extraction=False, cloud_verification=False)
        job = NaturalMemoryJob.objects.get(job_key=receipt['job_key']); self.assertEqual(job.status, 'stale')
        self.assertEqual(job.snapshot, {}); self.assertIsNone(self.state()['pending'])

    def test_source_edit_or_reset_between_reply_and_confirm_blocks_save(self):
        self.process(receipt := self.post()); body = self.confirm_body()
        ChatMessage.objects.filter(pk=receipt['message_id']).update(content='说的是我父亲。')
        result = self.client.post('/natural-memory/confirm/', body, format='json')
        self.assertEqual(result.status_code, 409); self.assertEqual(self.state()['entries'], [])
        self.assertIsNone(self.state()['pending'])

    def test_reset_and_excluded_source_block_worker(self):
        receipt = self.post(); job = NaturalMemoryJob.objects.get(job_key=receipt['job_key']); ex = extraction(job.snapshot)
        Conversation.objects.filter(pk=self.conv.pk).update(memory_version=F('memory_version') + 1)
        with self.assertRaisesMessage(NaturalMemoryContractError, 'stale_conversation'):
            intake.submit_reply(self.user, job.job_key, stage='extraction', response=ex)

    def test_read_is_nonwriting_and_unconfigured_read_does_not_initialize(self):
        before = list(NaturalMemoryLedger.objects.values()); count = NaturalMemoryOperation.objects.count()
        self.state(); self.state(); self.assertEqual(before, list(NaturalMemoryLedger.objects.values()))
        self.assertEqual(count, NaturalMemoryOperation.objects.count())
        self.client.force_authenticate(self.other); result = self.client.get('/natural-memory/state/')
        self.assertEqual(result.data['status'], 'unconfigured'); self.assertEqual(NaturalMemoryLedger.objects.count(), 1)

    def test_disabled_guard_and_database_guard_prevent_any_fixture_write(self):
        with override_settings(NATURAL_MEMORY_ISOLATED_ENABLED=False):
            result = self.client.post('/natural-memory/messages/', self.message_body(), format='json')
            self.assertEqual(result.status_code, 404)
        with override_settings(NATURAL_MEMORY_ISOLATED_DATABASE='wrong.sqlite3'):
            result = self.client.post('/natural-memory/messages/', self.message_body(), format='json')
            self.assertEqual(result.status_code, 400); self.assertEqual(result.data['reason'], 'isolated_database_required')
        self.assertEqual(ChatMessage.objects.count(), 0)

    def test_production_urls_do_not_mount_fixture(self):
        from django.urls import resolve, Resolver404
        with self.assertRaises(Resolver404):
            resolve('/natural-memory/messages/', urlconf='knowledge_graph_backend.urls')

    def test_failure_after_state_write_rolls_back_confirmation_and_receipt(self):
        self.process(self.post()); before = NaturalMemoryLedger.objects.get(user=self.user).state
        count = NaturalMemoryOperation.objects.count(); body = self.confirm_body(reply_content='确认。')
        with patch.object(intake, '_record', side_effect=RuntimeError('injected after state update')):
            with self.assertRaises(RuntimeError):
                self.client.post('/natural-memory/confirm/', body, format='json')
        self.assertEqual(NaturalMemoryLedger.objects.get(user=self.user).state, before)
        self.assertEqual(NaturalMemoryOperation.objects.count(), count)
        self.assertEqual(ChatMessage.objects.count(), 1)

    def test_two_candidates_second_failure_rolls_back_entire_batch(self):
        receipt = self.post('我有银屑病，也有湿疹。'); job = NaturalMemoryJob.objects.get(job_key=receipt['job_key'])
        a = disease(job.snapshot['messages'][-1]); b = disease(job.snapshot['messages'][-1], value='湿疹')
        self.process(receipt, items=[a, b]); body = self.confirm_body()
        before = NaturalMemoryLedger.objects.get(user=self.user).state
        from . import natural_memory_policy as decisions
        # No existing targets: fail the second entry's budget check instead.
        original = decisions.need; counter = {'entries': 0}
        def fault(condition, code):
            if code == 'entry_budget':
                counter['entries'] += 1
                # _locked and confirm each validate the existing aggregate;
                # the third check admits item one, the fourth fails item two.
                if counter['entries'] == 4:
                    raise RuntimeError('second entry injected failure')
            return original(condition, code)
        with patch.object(decisions, 'need', side_effect=fault):
            with self.assertRaises(RuntimeError):
                self.client.post('/natural-memory/confirm/', body, format='json')
        self.assertEqual(NaturalMemoryLedger.objects.get(user=self.user).state, before)
        self.assertEqual(self.state()['entries'], [])

    def test_forget_clears_extracted_bodies_jobs_and_replay_cannot_restore(self):
        entry = self.save_health(); pending_receipt = self.post(); job = NaturalMemoryJob.objects.get(job_key=pending_receipt['job_key'])
        ex = extraction(job.snapshot); intake.submit_reply(self.user, job.job_key, stage='extraction', response=ex)
        body = self.manage_body(entry); response = self.client.post('/natural-memory/manage/', body, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        row = NaturalMemoryLedger.objects.get(user=self.user)
        self.assertNotIn('银屑病', json.dumps(row.state, ensure_ascii=False)); self.assertEqual(ChatMessage.objects.count(), 2)
        self.assertFalse(self.state()['entries'][0]['eligible'])
        self.assertFalse(NaturalMemoryJob.objects.exclude(snapshot={}).exists())
        self.assertFalse(NaturalMemoryJob.objects.exclude(extraction_reply='').exists())
        replay = self.client.post('/natural-memory/manage/', body, format='json')
        self.assertEqual(replay.data['status'], 'replayed'); self.assertFalse(replay.data['saved_ack'])

    def test_confirm_replay_after_forget_is_metadata_only(self):
        self.process(self.post()); body = self.confirm_body(); self.client.post('/natural-memory/confirm/', body, format='json')
        entry = self.state()['entries'][0]; self.client.post('/natural-memory/manage/', self.manage_body(entry), format='json')
        response = self.client.post('/natural-memory/confirm/', body, format='json')
        self.assertEqual(response.data['status'], 'replayed'); self.assertFalse(response.data['saved_ack'])
        self.assertEqual(self.state()['entries'][0]['status'], 'forgotten')

    def test_preference_no_auto_until_explicit_synthetic_gate(self):
        receipt = self.post('以后简短一点。'); source = NaturalMemoryJob.objects.get(job_key=receipt['job_key']).snapshot
        result = self.process(receipt, items=[preference(source)])
        self.assertEqual(result['status'], 'needs_confirmation'); self.assertFalse(result['saved_ack'])

    def test_targeted_scope_reply_closes_question_in_one_new_message(self):
        receipt = self.post('以后详细，但只限这一条。'); job = NaturalMemoryJob.objects.get(job_key=receipt['job_key'])
        self.process(receipt, items=[], questions=[question(job.snapshot)])
        display = self.state()['pending']['display_digest']
        reply = self.post('只是这一条详细。', mode='clarification', display_digest=display, context_ids=[receipt['message_id']])
        self.assertIsNone(self.state()['pending'])  # hide the old card while the reply is processing
        result = self.process(reply, items=[])
        self.assertEqual(result['status'], 'projected_resolved_without_change'); self.assertFalse(result['saved_ack'])
        self.assertIsNone(self.state()['pending']); self.assertEqual(self.state()['entries'], [])

    def test_migrations_match_registered_models(self):
        from django.db.migrations.loader import MigrationLoader
        from django.db.migrations.autodetector import MigrationAutodetector
        from django.db.migrations.state import ProjectState
        loader = MigrationLoader(connection)
        changes = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(__import__('django').apps.apps)).changes(graph=loader.graph)
        self.assertNotIn('api', changes)

    def test_expired_job_cannot_request_or_accept_new_model_reply(self):
        receipt = self.post(); job = NaturalMemoryJob.objects.get(job_key=receipt['job_key']); ex = extraction(job.snapshot)
        NaturalMemoryJob.objects.filter(pk=job.pk).update(deadline_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaisesMessage(NaturalMemoryContractError, 'job_deadline'):
            intake.worker_request(self.user, job.job_key)
        with self.assertRaisesMessage(NaturalMemoryContractError, 'job_deadline'):
            intake.submit_reply(self.user, job.job_key, stage='extraction', response=ex)
        self.assertEqual(self.state()['entries'], [])

    def test_other_account_cannot_read_or_complete_owned_job(self):
        receipt = self.post(); job = NaturalMemoryJob.objects.get(job_key=receipt['job_key']); ex = extraction(job.snapshot)
        intake.initialize_account(self.other)
        with self.assertRaisesMessage(NaturalMemoryContractError, 'job_unavailable'):
            intake.submit_reply(self.other, job.job_key, stage='extraction', response=ex)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get('/natural-memory/state/').data['entries'], [])

    def test_external_source_exclusion_blocks_commit(self):
        self.process(receipt := self.post()); body = self.confirm_body()
        MemorySourceExclusion.objects.create(user=self.user, message_id=receipt['message_id'])
        response = self.client.post('/natural-memory/confirm/', body, format='json')
        self.assertEqual(response.status_code, 409); self.assertFalse(response.data['saved_ack'])
        self.assertEqual(self.state()['entries'], [])

    def test_collection_off_still_persists_message_without_model_job(self):
        row = NaturalMemoryLedger.objects.get(user=self.user)
        intake.configure(self.user, expected_epoch=row.epoch, expected_policy_version=row.policy['version'],
            collect_types=[], use_enabled=False, cloud_extraction=False, cloud_verification=False)
        receipt = self.post()
        self.assertEqual(receipt['status'], 'stored_without_collection'); self.assertIsNone(receipt['job_key'])
        self.assertFalse(receipt['saved_ack']); self.assertEqual(ChatMessage.objects.count(), 1)
        self.assertEqual(NaturalMemoryJob.objects.count(), 0)

    def test_review_permission_denial_never_saves_health(self):
        row = NaturalMemoryLedger.objects.get(user=self.user)
        intake.configure(self.user, expected_epoch=row.epoch, expected_policy_version=row.policy['version'],
            collect_types=['self_disease_report'], use_enabled=True, cloud_extraction=True, cloud_verification=False)
        receipt = self.post(); job = NaturalMemoryJob.objects.get(job_key=receipt['job_key'])
        options = {**args(job.snapshot), 'permissions': NaturalMemoryLedger.objects.get(user=self.user).permissions}
        result = intake.submit_reply(self.user, job.job_key, stage='extraction', response=extraction(job.snapshot, options=options))
        self.assertEqual(result['status'], 'blocked'); self.assertFalse(result['saved_ack'])
        self.assertEqual(self.state()['entries'], [])

    def test_two_items_commit_in_single_receipt_after_accept(self):
        receipt = self.post('我有银屑病，也有湿疹。'); job = NaturalMemoryJob.objects.get(job_key=receipt['job_key'])
        self.process(receipt, items=[disease(job.snapshot['messages'][-1]), disease(job.snapshot['messages'][-1], value='湿疹')])
        response = self.accept()
        self.assertTrue(response['saved_ack']); self.assertEqual(len(self.state()['entries']), 2)
        self.assertEqual(NaturalMemoryOperation.objects.filter(receipt__saved_ack=True).count(), 1)

    def test_confirmation_replay_does_not_duplicate_reply_message(self):
        self.process(self.post()); body = self.confirm_body(reply_content='确认保存。')
        self.client.post('/natural-memory/confirm/', body, format='json')
        response = self.client.post('/natural-memory/confirm/', body, format='json')
        self.assertEqual(response.data['status'], 'replayed'); self.assertEqual(ChatMessage.objects.count(), 2)
        self.assertEqual(len(self.state()['entries']), 1)

    def test_only_explicit_context_is_read_and_not_entire_history(self):
        old = ChatMessage.objects.create(conversation=self.conv, role='user', content='不应默读的虚构旧消息')
        receipt = self.post(); job = NaturalMemoryJob.objects.get(job_key=receipt['job_key'])
        self.assertEqual(len(job.snapshot['messages']), 1)
        self.assertNotIn(str(old.pk), [m['id'].split(':')[1] for m in job.snapshot['messages']])
        self.assertNotIn('不应默读', json.dumps(intake.worker_request(self.user, job.job_key), ensure_ascii=False))

    def test_persisted_job_audit_keeps_versions_hashes_without_completed_raw(self):
        receipt = self.post(); self.process(receipt)
        job = NaturalMemoryJob.objects.get(job_key=receipt['job_key'])
        self.assertEqual(job.snapshot, {}); self.assertEqual(job.extraction_reply, '')
        self.assertTrue(job.extraction_hash); self.assertTrue(job.review_hash)
        self.assertEqual(job.audit['review_outcomes'][0]['verdict'], 'supported')
        self.assertNotIn('银屑病', json.dumps(job.audit, ensure_ascii=False))

    def test_job_creation_failure_rolls_back_message_and_generation(self):
        before = NaturalMemoryLedger.objects.get(user=self.user).state
        with patch.object(NaturalMemoryJob.objects, 'create', side_effect=RuntimeError('fictional queue write failure')):
            with self.assertRaises(RuntimeError):
                self.client.post('/natural-memory/messages/', self.message_body(), format='json')
        self.assertEqual(ChatMessage.objects.count(), 0)
        self.assertEqual(NaturalMemoryLedger.objects.get(user=self.user).state, before)
        self.assertEqual(NaturalMemoryOperation.objects.count(), 0)


# Imported explicitly for the reset test; no runtime caller uses a test module.
from django.db.models import F
