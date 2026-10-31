"""R7 isolated panel adapter. No production URL, SDK, or semantic rules.

User choices never supply candidates/model replies or the semantic release gate.
All state changes reuse R3-R6 checks and invalidate old derived content.
"""
import copy
import json
from django.conf import settings
from django.db import transaction
from .models import Conversation
from .natural_memory_models import NaturalMemoryLedger, NaturalMemoryJob
from .natural_memory_contract import fields, need, digest, validate_candidate, NaturalMemoryContractError
from . import natural_memory_intake as intake
from . import natural_memory_answering as answering


FLAGS = {'collect_health', 'collect_preferences', 'use_enabled', 'cloud_extraction',
    'cloud_verification', 'cloud_routing', 'cloud_answer_personal_context', 'preference_auto_enabled'}
VERSIONS = {'expected_revision', 'expected_epoch', 'expected_policy_version', 'expected_answer_grant_version'}


def enabled(user):
    intake.account(user)
    need(getattr(settings, 'NATURAL_MEMORY_PANEL_ENABLED', False) is True, 'isolated_disabled')


def read_panel(user):
    enabled(user)
    with transaction.atomic():
        row = NaturalMemoryLedger.objects.filter(user=user).first()
        need(row is not None, 'account_not_configured')
        state = intake.read_state(user)
        need(state['revision'] == row.revision and state['epoch'] == row.epoch, 'read_changed')
        grant = answering.grants(user) or {'version': 0, 'cloud_routing': False, 'cloud_answer_personal_context': False}
        pending = row.state['pending'] if state['pending'] else None
        # Only explicit pending provenance, never a historical chat scan.
        context = [int(m['id'].split(':')[1]) for m in pending['source']['messages']] if pending else []
        conversations = list(Conversation.objects.filter(user=user).order_by('-pk').values('id', 'title', 'memory_version')[:32])
        result = {**state, 'owner': intake.account(user), 'answer_grant_version': grant['version'],
            'settings': {'collect_health': 'self_disease_report' in row.policy['collect_types'],
                'collect_preferences': 'presentation_preference' in row.policy['collect_types'],
                **{k: row.policy[k] for k in ['use_enabled', 'preference_auto_enabled']},
                **{k: row.permissions[k] for k in ['cloud_extraction', 'cloud_verification']},
                **{k: grant[k] for k in ['cloud_routing', 'cloud_answer_personal_context']}},
            'preference_auto_gate_passed': row.policy['preference_auto_gate_passed'],
            'conversations': conversations, 'pending_session': pending['source']['session'] if pending else None,
            'clarification_context_ids': context if len(context) <= 4 else [],
            'clarification_available': bool(pending and len(context) <= 4)}
        need(NaturalMemoryLedger.objects.filter(pk=row.pk, revision=row.revision, epoch=row.epoch).exists(), 'read_changed')
        return result


def update_settings(user, body):
    enabled(user); fields(body, FLAGS | VERSIONS | {'request_key'}, 'panel_settings_fields')
    need(all(type(body[k]) is bool for k in FLAGS), 'panel_settings_flags')
    intake._key(body['request_key']); fingerprint = digest(['panel-settings', body])
    with intake._locked(user) as row:
        replay = intake._replay(user, body['request_key'], fingerprint)
        if replay is not None:
            return replay
        intake._check(row, body)
        grant = answering.grants(user)
        need(type(body['expected_answer_grant_version']) is int and
            body['expected_answer_grant_version'] == (grant['version'] if grant else 0), 'stale_answer_grant')
        types = [category for flag, category in [('collect_health', 'self_disease_report'),
            ('collect_preferences', 'presentation_preference')] if body[flag]]
        # Keep the trusted release gate. The browser cannot set it.
        intake.configure(user, expected_epoch=row.epoch, expected_policy_version=row.policy['version'],
            collect_types=types, use_enabled=body['use_enabled'], cloud_extraction=body['cloud_extraction'],
            cloud_verification=body['cloud_verification'], preference_auto_enabled=body['preference_auto_enabled'],
            preference_auto_gate_passed=row.policy['preference_auto_gate_passed'])
        row.refresh_from_db()
        answering.configure_grants(user, expected_epoch=row.epoch,
            expected_version=body['expected_answer_grant_version'], cloud_routing=body['cloud_routing'],
            cloud_answer_personal_context=body['cloud_answer_personal_context'])
        row.refresh_from_db()
        return intake._record(row, body['request_key'], fingerprint, 'settings_updated')


def new_conversation(user, body):
    enabled(user); fields(body, {'request_key', 'expected_revision', 'expected_epoch', 'expected_policy_version'}, 'panel_conversation_fields')
    intake._key(body['request_key']); fingerprint = digest(['panel-conversation', body])
    with intake._locked(user) as row:
        replay = intake._replay(user, body['request_key'], fingerprint)
        if replay is not None:
            return replay
        intake._check(row, body)
        need(Conversation.objects.filter(user=user).count() < 32, 'panel_conversation_budget')
        conv = Conversation.objects.create(user=user, title='新的隔离会话')
        return intake._record(row, body['request_key'], fingerprint, 'conversation_created', conversation_id=conv.pk)


def process_message(user, body):
    """Persist first, then call explicitly injected offline worker, never a live SDK.

    Relation choice is explicit user intent bound to the posted versions and a
    single server-validated candidate. A failure keeps the received message.
    """
    enabled(user)
    fields(body, {'message', 'relation'}, 'panel_message_fields')
    relation = body['relation']
    need(relation is None or type(relation) is dict, 'panel_relation')
    need(type(body['message']) is dict and 'request_key' in body['message'], 'panel_message')
    intake._key(body['message']['request_key'])
    with intake._locked(user):
        replay = intake._replay(user, body['message']['request_key'], digest(['message', body['message']]))
        if replay is not None:
            return replay
    if relation is not None:
        fields(relation, {'operation', 'target', 'target_version'}, 'panel_relation_fields')
        need(relation['operation'] in {'correction', 'new_event'}, 'panel_relation_operation')
        if relation['operation'] == 'new_event':
            need(relation['target'] is None and relation['target_version'] is None, 'choice_target')
        else:
            with intake._locked(user) as row:
                intake._check(row, body['message'])
                entry = row.state['entries'].get(relation['target'])
                need(entry is not None and entry['version'] == relation['target_version'], 'stale_target')
    result = intake.post_message(user, body['message'])
    if not result.get('job_key') or result['status'] == 'replayed':
        return result
    key = result['job_key']
    provider = getattr(settings, 'NATURAL_MEMORY_PANEL_WORKER', None)
    if getattr(provider, 'offline_fixture', False) is not True:
        return {**result, 'processing_status': 'worker_not_configured', 'saved_ack': False}
    try:
        request = intake.worker_request(user, key)
        ex = provider.extract(request)
        if relation is not None:
            items = json.loads(ex).get('items', [])
            need(len(items) == 1 and items[0].get('category') == 'self_disease_report', 'panel_relation_health_only')
        first = intake.submit_reply(user, key, stage='extraction', response=ex)
        need(relation is None or first['status'] == 'awaiting_review', 'panel_relation_requires_review')
        if first['status'] == 'awaiting_review':
            request = intake.worker_request(user, key)
            rv = provider.review(request)
            choices = None
            if relation is not None:
                with intake._locked(user) as row:
                    job = intake._job(row, key)
                    from .natural_memory_extraction import preview
                    live = intake._sources(row, job.snapshot)
                    options = {'permissions': row.permissions, 'expected_policy_version': row.policy['version'],
                        'expected_conversation_version': live['conversation_versions'][job.snapshot['session']],
                        'expected_context_digest': job.snapshot['context_digest']}
                    proposed = preview(job.snapshot, extraction_response=ex, review_response=rv, **options)
                    supported = proposed.get('supported', [])
                    need(len(supported) == 1, 'panel_relation_single_candidate')
                    # Re-derive preview ID from a server-validated candidate.
                    candidate = supported[0]['grounded']['candidate']
                    grounded = validate_candidate(candidate, job.snapshot, **{k: v for k, v in options.items() if k != 'permissions'})
                    choices = {grounded['preview_id']: {**copy.deepcopy(relation), 'event_state': None}}
            first = intake.submit_reply(user, key, stage='review', response=rv, choices=choices)
        return {**result, 'processing_status': first['status'], 'saved_ack': first['saved_ack']}
    except (NaturalMemoryContractError, ValueError, TypeError, KeyError, RuntimeError):
        with intake._locked(user) as row:
            job = intake._job(row, key)
            if job.status in {'awaiting_extraction', 'awaiting_review'}:
                job.status = 'failed'; job.reason = 'panel_processing_failed'; job.snapshot = {}; job.extraction_reply = ''
                job.save()
        return {**result, 'processing_status': 'processing_failed', 'saved_ack': False}
