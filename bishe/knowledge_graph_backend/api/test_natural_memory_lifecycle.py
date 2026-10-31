"""R6 isolated invalidation, continuation, erasure and consumer checks."""
import copy
import json
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from django.db.models import F

from . import test_natural_memory_answering as fixtures
from . import natural_memory_lifecycle as lifecycle
from . import natural_memory_answering as answering
from . import natural_memory_intake as intake
from .natural_memory_models import NaturalMemoryLedger, NaturalMemoryDerived, NaturalMemoryOperation, NaturalMemoryJob
from .models import ChatMessage, Conversation, MemorySourceExclusion
from .natural_memory_contract import NaturalMemoryContractError, validate_candidate
from .test_natural_memory_contract import disease
from .test_natural_memory_extraction import extraction, review


@override_settings(ROOT_URLCONF='api.natural_memory_fixture_urls', NATURAL_MEMORY_ISOLATED_ENABLED=True,
    NATURAL_MEMORY_ISOLATED_DATABASE='memory')
class NaturalMemoryLifecycleTests(TestCase):
    setUp = fixtures.NaturalMemoryAnsweringTests.setUp
    body = fixtures.NaturalMemoryAnsweringTests.body
    source = fixtures.NaturalMemoryAnsweringTests.source
    save_health = fixtures.NaturalMemoryAnsweringTests.save_health
    ask = fixtures.NaturalMemoryAnsweringTests.ask

    def start(self, paused=False):
        self.save_health(); spy = fixtures.OfflineProviders(self.source)
        conv = Conversation.objects.create(user=self.user)
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=spy):
            response = self.client.post('/natural-memory/questions/pause/' if paused else '/natural-memory/questions/',
                self.body(conv=conv), format='json')
        self.assertEqual(response.status_code, 200, response.data)
        return response.data, spy

    def audit(self, data):
        return NaturalMemoryOperation.objects.get(user=self.user, request_key='r5-answer-' + str(data['message_id'])).receipt

    def change(self, action='forget'):
        state = intake.read_state(self.user); entry = next(e for e in state['entries'] if e['status'] != 'forgotten')
        row = NaturalMemoryLedger.objects.get(user=self.user)
        response = self.client.post('/natural-memory/manage/', {'request_key': 'r6-' + action,
            'conversation_id': self.conv.pk, 'target': entry['key'], 'target_version': entry['version'],
            'display_digest': entry['display_digest'], 'action': action, 'expected_revision': row.revision,
            'expected_epoch': row.epoch, 'expected_policy_version': row.policy['version']}, format='json')
        self.assertEqual(response.status_code, 200, response.data); return response.data

    def configure(self, *, collect=None, use=True, extraction=True, verification=True):
        row = NaturalMemoryLedger.objects.get(user=self.user)
        return intake.configure(self.user, expected_epoch=row.epoch, expected_policy_version=row.policy['version'],
            collect_types=row.policy['collect_types'] if collect is None else collect, use_enabled=use,
            cloud_extraction=extraction, cloud_verification=verification)

    def resume_body(self, data):
        paused = data['answer_result']
        return {'checkpoint_key': paused['checkpoint_key'], 'expected_revision': paused['revision'], 'expected_epoch': paused['epoch']}

    def test_actual_answer_creates_fenced_cache_summary_and_display(self):
        data, _ = self.start(); audit = self.audit(data)
        for field, kind in [('cache_key', 'cache'), ('summary_key', 'summary')]:
            result = lifecycle.read_derived(self.user, audit[field], kind=kind)
            self.assertEqual(result['status'], 'ready'); self.assertIn('银屑病', json.dumps(result['payload'], ensure_ascii=False))
        response = self.client.get('/natural-memory/results/' + audit['delivery_key'] + '/')
        self.assertEqual(response.status_code, 200); self.assertEqual(response.data['status'], 'answered')
        self.assertEqual(response.data['fence']['owner'], 'user:' + str(self.user.pk))

    def test_forget_atomically_scrubs_all_actual_derived_payloads_and_keeps_chats(self):
        data, _ = self.start(); audit = self.audit(data); chats = list(ChatMessage.objects.values_list('id', 'content'))
        self.change('forget')
        self.assertFalse(NaturalMemoryDerived.objects.exclude(payload={}).exists())
        self.assertEqual(chats, list(ChatMessage.objects.values_list('id', 'content')))
        self.assertNotIn('银屑病', json.dumps(NaturalMemoryLedger.objects.get(user=self.user).state, ensure_ascii=False))
        for field, kind in [('cache_key', 'cache'), ('summary_key', 'summary'), ('delivery_key', 'answer_view')]:
            self.assertIsNone(lifecycle.read_derived(self.user, audit[field], kind=kind)['payload'])
        response = self.client.get('/natural-memory/results/' + audit['delivery_key'] + '/')
        self.assertEqual(response.status_code, 410); self.assertIsNone(response.data['answer'])

    def test_pause_resume_runs_answer_once_without_rerouting(self):
        data, spy = self.start(paused=True); audit = self.audit(data)
        self.assertEqual(data['answer_status'], 'answer_paused'); self.assertEqual(len(spy.routing_inputs), 1)
        self.assertFalse(spy.answer_inputs); body = self.resume_body(data)
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=spy):
            response = self.client.post('/natural-memory/resume/', body, format='json')
            again = self.client.post('/natural-memory/resume/', body, format='json')
        self.assertEqual(response.status_code, 200, response.data); self.assertEqual(response.data['status'], 'answered')
        self.assertEqual(len(spy.routing_inputs), 1); self.assertEqual(len(spy.answer_inputs), 1)
        self.assertIsNone(again.data['answer'])
        current = self.audit(data); self.assertEqual(current['answer_calls'], 1)
        self.assertEqual(current['deadline_at'], audit['deadline_at'])

    def test_forget_blocks_old_checkpoint_without_model_or_budget_reset(self):
        data, spy = self.start(paused=True); body = self.resume_body(data); old = self.audit(data)
        self.change('forget')
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=spy):
            response = self.client.post('/natural-memory/resume/', body, format='json')
        self.assertIsNone(response.data['answer']); self.assertFalse(spy.answer_inputs)
        new = self.audit(data); self.assertEqual(new['deadline_at'], old['deadline_at'])
        self.assertEqual(new['routing_calls'], 1); self.assertEqual(new['answer_calls'], 0)
        self.assertFalse(NaturalMemoryDerived.objects.exclude(payload={}).exists())

    def test_expired_checkpoint_does_not_receive_another_ninety_seconds(self):
        data, spy = self.start(paused=True); body = self.resume_body(data)
        row = NaturalMemoryDerived.objects.get(artifact_key=body['checkpoint_key'])
        row.expires_at = timezone.now() - timedelta(seconds=1)
        row.seal = lifecycle._seal(row.kind, row.binding, row.payload_hash, row.expires_at); row.save()
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=spy):
            response = self.client.post('/natural-memory/resume/', body, format='json')
        self.assertEqual(response.data['reason'], 'derived_expired'); self.assertFalse(spy.answer_inputs)
        row.refresh_from_db(); self.assertEqual(row.payload, {})

    def test_foreign_owner_cannot_read_or_resume(self):
        data, spy = self.start(paused=True); intake.initialize_account(self.other)
        self.client.force_authenticate(self.other)
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=spy):
            response = self.client.post('/natural-memory/resume/', self.resume_body(data), format='json')
        self.assertEqual(response.data['reason'], 'derived_unavailable'); self.assertFalse(spy.answer_inputs)

    def test_payload_and_binding_tampering_are_rejected_and_scrubbed(self):
        data, _ = self.start(); audit = self.audit(data)
        NaturalMemoryDerived.objects.filter(artifact_key=audit['cache_key']).update(payload={'forged': 'payload'})
        result = lifecycle.read_derived(self.user, audit['cache_key'], kind='cache')
        self.assertEqual(result['reason'], 'derived_payload_changed')
        row = NaturalMemoryDerived.objects.get(artifact_key=audit['summary_key']); row.binding['epoch'] += 1; row.save()
        result = lifecycle.read_derived(self.user, row.artifact_key, kind='summary')
        self.assertEqual(result['reason'], 'derived_binding_changed')
        row.refresh_from_db(); self.assertEqual(row.payload, {})

    def test_source_edit_without_version_bump_invalidates_cached_inputs(self):
        data, _ = self.start(); audit = self.audit(data)
        ChatMessage.objects.filter(conversation=self.conv, role='user').update(content='原句改为说我父亲。')
        for name, kind in [('cache_key', 'cache'), ('summary_key', 'summary'), ('delivery_key', 'answer_view')]:
            result = lifecycle.read_derived(self.user, audit[name], kind=kind)
            self.assertEqual(result['reason'], 'derived_context_changed'); self.assertIsNone(result['payload'])

    def test_external_exclusion_and_conversation_reset_are_checked_on_read(self):
        data, _ = self.start(); audit = self.audit(data)
        mid = ChatMessage.objects.filter(conversation=self.conv, role='user').first().pk
        MemorySourceExclusion.objects.create(user=self.user, message_id=mid)
        self.assertIsNone(lifecycle.read_derived(self.user, audit['summary_key'], kind='summary')['payload'])
        Conversation.objects.filter(pk=self.conv.pk).update(memory_version=F('memory_version') + 1)
        self.assertIsNone(lifecycle.read_derived(self.user, audit['cache_key'], kind='cache')['payload'])

    def test_reset_endpoint_blocks_old_source_but_preserves_chat(self):
        data, _ = self.start(); audit = self.audit(data); row = NaturalMemoryLedger.objects.get(user=self.user)
        count = ChatMessage.objects.count()
        response = self.client.post('/natural-memory/reset/', {'request_key': 'r6-reset', 'conversation_id': self.conv.pk,
            'expected_revision': row.revision, 'expected_epoch': row.epoch, 'expected_policy_version': row.policy['version'],
            'expected_conversation_version': 0}, format='json')
        self.assertEqual(response.status_code, 200, response.data); self.assertEqual(ChatMessage.objects.count(), count)
        self.assertFalse(intake.read_state(self.user)['entries'][0]['eligible'])
        self.assertIsNone(lifecycle.read_derived(self.user, audit['summary_key'], kind='summary')['payload'])

    def test_collection_off_keeps_saved_use_but_invalidates_old_derivatives(self):
        data, _ = self.start(); audit = self.audit(data); self.configure(collect=[])
        self.assertTrue(intake.read_state(self.user)['entries'][0]['eligible'])
        self.assertIsNone(lifecycle.read_derived(self.user, audit['cache_key'], kind='cache')['payload'])
        receipt = intake.post_message(self.user, self.body('新虚构自述'))
        self.assertEqual(receipt['status'], 'stored_without_collection')

    def test_use_off_stops_background_while_collection_remains_enabled(self):
        data, _ = self.start(); self.configure(use=False)
        self.assertFalse(intake.read_state(self.user)['entries'][0]['eligible'])
        row = NaturalMemoryLedger.objects.get(user=self.user); self.assertTrue(row.policy['collect_types'])
        self.assertFalse(NaturalMemoryDerived.objects.exclude(payload={}).exists())

    def test_each_processing_permission_revocation_invalidates_old_checkpoint(self):
        data, spy = self.start(paused=True); body = self.resume_body(data)
        self.configure(extraction=False)
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=spy):
            response = self.client.post('/natural-memory/resume/', body, format='json')
        self.assertIsNone(response.data['answer']); self.assertTrue(intake.read_state(self.user)['entries'][0]['eligible'])
        self.configure(extraction=True, verification=False)
        self.assertFalse(NaturalMemoryDerived.objects.exclude(payload={}).exists())

    def test_answer_and_routing_permission_revocation_invalidate_derivatives(self):
        data, _ = self.start(); row = NaturalMemoryLedger.objects.get(user=self.user)
        answering.configure_grants(self.user, expected_version=1, expected_epoch=row.epoch,
            cloud_routing=False, cloud_answer_personal_context=False)
        self.assertFalse(NaturalMemoryDerived.objects.exclude(payload={}).exists())
        self.assertTrue(intake.read_state(self.user)['entries'][0]['eligible'])

    def test_forget_still_works_after_collection_and_cloud_permissions_off(self):
        self.start(); self.configure(collect=[], extraction=False, verification=False)
        self.change('forget'); self.assertEqual(intake.read_state(self.user)['entries'][0]['status'], 'forgotten')
        self.assertFalse(NaturalMemoryDerived.objects.exclude(payload={}).exists())

    def test_inflight_forget_cannot_return_old_answer(self):
        self.save_health(); spy = fixtures.OfflineProviders(self.source); spy.after_answer = lambda: self.change('forget')
        data, _ = self.ask(providers=spy)
        self.assertIsNone(data['answer_result']['answer']); self.assertEqual(data['answer_status'], 'answer_unavailable')
        self.assertFalse(ChatMessage.objects.filter(role='assistant').exists())

    def test_delivery_recheck_after_commit_drops_stale_HTTP_body(self):
        self.save_health(); real = lifecycle.read_delivery; fired = {'value': False}
        def intervene(user, key):
            if not fired['value']:
                fired['value'] = True; self.change('forget')
            return real(user, key)
        with patch.object(lifecycle, 'read_delivery', side_effect=intervene):
            data, _ = self.ask()
        self.assertEqual(data['answer_status'], 'answer_unavailable'); self.assertIsNone(data['answer_result']['answer'])
        self.assertEqual(ChatMessage.objects.filter(role='assistant').count(), 1)  # retained chat, not reusable current result

    def test_atomic_failure_during_invalidation_rolls_back_forget(self):
        data, _ = self.start(); before = copy.deepcopy(NaturalMemoryLedger.objects.get(user=self.user).state)
        real = lifecycle.invalidate_locked
        def fault(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError('injected after derivative erasure')
        with patch.object(lifecycle, 'invalidate_locked', side_effect=fault):
            with self.assertRaises(RuntimeError):
                self.change('forget')
        self.assertEqual(NaturalMemoryLedger.objects.get(user=self.user).state, before)
        self.assertTrue(NaturalMemoryDerived.objects.filter(status='ready').exists())

    def test_old_legacy_checkpoint_tokens_cannot_enter_new_resume(self):
        data, spy = self.start(paused=True)
        body = self.resume_body(data); body['checkpoint_key'] = 'legacy-signed-token'
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=spy):
            response = self.client.post('/natural-memory/resume/', body, format='json')
        self.assertEqual(response.status_code, 400); self.assertFalse(spy.answer_inputs)

    def test_checkpoint_claim_survives_exception_without_reopening_budget(self):
        data, spy = self.start(paused=True); body = self.resume_body(data)
        spy.before_answer = lambda: (_ for _ in ()).throw(RuntimeError('fixture interruption'))
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=spy):
            one = self.client.post('/natural-memory/resume/', body, format='json')
            two = self.client.post('/natural-memory/resume/', body, format='json')
        self.assertEqual(one.data['status'], 'answer_failed'); self.assertIsNone(two.data['answer'])
        self.assertEqual(len(spy.answer_inputs), 1)
        self.assertEqual(self.audit(data)['answer_calls'], 1)

    def test_new_user_message_scrubs_old_summary_and_result(self):
        data, _ = self.start(); audit = self.audit(data)
        self.client.post('/natural-memory/messages/', self.body('换个话题。'), format='json')
        self.assertFalse(NaturalMemoryDerived.objects.exclude(payload={}).exists())
        self.assertIsNone(lifecycle.read_derived(self.user, audit['summary_key'], kind='summary')['payload'])

    def test_wrong_kind_does_not_expose_checkpoint_as_summary(self):
        data, _ = self.start(paused=True)
        result = lifecycle.read_derived(self.user, data['answer_result']['checkpoint_key'], kind='summary')
        self.assertEqual(result['reason'], 'derived_kind'); self.assertIsNone(result['payload'])

    def test_new_explicit_source_after_forget_does_not_unblock_old_sources(self):
        self.start(); self.change('forget')
        old = set(NaturalMemoryLedger.objects.get(user=self.user).state['blocked_sources'])
        posted = self.client.post('/natural-memory/messages/', self.body('重新保存：我可能有银屑病。'), format='json')
        self.assertEqual(posted.status_code, 200); job = NaturalMemoryJob.objects.get(job_key=posted.data['job_key'])
        ex = extraction(job.snapshot, [disease(job.snapshot['messages'][-1], certainty='possible')])
        intake.submit_reply(self.user, job.job_key, stage='extraction', response=ex)
        intake.submit_reply(self.user, job.job_key, stage='review', response=review(job.snapshot, ex))
        state = intake.read_state(self.user)
        self.assertTrue(state['pending']); self.assertFalse(any(e['eligible'] for e in state['entries']))
        self.assertEqual(old, set(NaturalMemoryLedger.objects.get(user=self.user).state['blocked_sources']))

    def test_production_new_routes_absent(self):
        from django.urls import resolve, Resolver404
        for path in ['/natural-memory/resume/', '/natural-memory/reset/', '/natural-memory/results/' + 'a' * 32 + '/']:
            with self.assertRaises(Resolver404):
                resolve(path, urlconf='knowledge_graph_backend.urls')

    def stage_update(self, text, operation, value='银屑病'):
        posted = self.client.post('/natural-memory/messages/', self.body(text), format='json')
        job = NaturalMemoryJob.objects.get(job_key=posted.data['job_key']); source = job.snapshot
        candidate = disease(source['messages'][-1], value=value, certainty='possible')
        candidate['relation'] = 'correction' if operation == 'correction' else 'new_information'
        ex = extraction(source, [candidate]); intake.submit_reply(self.user, job.job_key, stage='extraction', response=ex)
        row = NaturalMemoryLedger.objects.get(user=self.user); target = next(e for e in row.state['entries'].values() if e['status'] == 'active')
        from .test_natural_memory_extraction import args
        grounded = validate_candidate(candidate, source, **{k: v for k, v in args(source).items() if k != 'permissions'})
        choice = {grounded['preview_id']: {'operation': operation, 'target': target['key'],
            'target_version': target['version'], 'event_state': None}}
        intake.submit_reply(self.user, job.job_key, stage='review', response=review(source, ex), choices=choice)
        state = intake.read_state(self.user); row = NaturalMemoryLedger.objects.get(user=self.user)
        result = self.client.post('/natural-memory/confirm/', {'request_key': 'r6-confirm-update',
            'conversation_id': self.conv.pk, 'display_digest': state['pending']['display_digest'], 'decision': 'accept',
            'reply_content': None, 'expected_revision': row.revision, 'expected_epoch': row.epoch,
            'expected_policy_version': row.policy['version']}, format='json')
        self.assertEqual(result.status_code, 200, result.data)

    def test_correction_keeps_new_version_and_blocks_older_checkpoint(self):
        data, spy = self.start(paused=True); body = self.resume_body(data)
        self.stage_update('更正：我可能有湿疹，之前银屑病说错了。', 'correction', '湿疹')
        entry = intake.read_state(self.user)['entries'][0]
        self.assertEqual(entry['fact']['content']['value_raw'], '湿疹'); self.assertTrue(entry['eligible'])
        with override_settings(NATURAL_MEMORY_FIXTURE_PROVIDERS=spy):
            resumed = self.client.post('/natural-memory/resume/', body, format='json')
        self.assertIsNone(resumed.data['answer']); self.assertFalse(spy.answer_inputs)
        self.assertEqual(intake.read_state(self.user)['entries'][0]['fact']['content']['value_raw'], '湿疹')

    def test_forget_excludes_all_linked_duplicate_message_sources(self):
        self.save_health(); self.stage_update('再次说明，我可能有银屑病。', 'duplicate')
        sources = {q['message_id'] for q in intake.read_state(self.user)['entries'][0]['sources']}
        self.assertEqual(len(sources), 2)
        self.ask(); self.change('forget')
        row = NaturalMemoryLedger.objects.get(user=self.user)
        self.assertTrue(sources <= set(row.state['blocked_sources']))
        self.assertFalse(NaturalMemoryDerived.objects.exclude(payload={}).exists())
