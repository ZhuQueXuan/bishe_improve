"""R4 isolated authenticated intake and durable decision commit.

No model dispatcher. Worker reply methods are trusted internal interfaces, never
HTTP payloads. The fixture URLconf is deliberately absent from production URLs.
"""
import copy
from contextlib import contextmanager
from pathlib import Path
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction, IntegrityError, OperationalError
from django.db.models import F
from django.utils import timezone

from .models import ChatMessage, Conversation, MemorySourceExclusion
from .natural_memory_models import NaturalMemoryLedger, NaturalMemoryJob, NaturalMemoryOperation
from .natural_memory_contract import (NaturalMemoryContractError, TYPES, MESSAGE_FIELDS,
    capture_context, digest, fields, need, text)
from .natural_memory_extraction import preview, prepare_extraction, prepare_review
from . import natural_memory_policy as decisions


def isolated():
    """Two explicit fixture settings, plus an allowlisted disposable DB location."""
    if (getattr(settings, 'NATURAL_MEMORY_FORMAL_ENABLED', False) is True and
            getattr(settings, 'NATURAL_MEMORY_RELEASE_READY', False) is True):
        selected = getattr(settings, 'NATURAL_MEMORY_FORMAL_DATABASE', None)
        need(type(selected) is str and selected == str(connection.settings_dict['NAME']), 'formal_database_required')
        return
    need(getattr(settings, 'NATURAL_MEMORY_ISOLATED_ENABLED', False) is True, 'isolated_disabled')
    configured = str(connection.settings_dict['NAME'])
    selected = getattr(settings, 'NATURAL_MEMORY_ISOLATED_DATABASE', None)
    if selected == 'memory':
        need(configured == ':memory:' or configured.startswith('file:memorydb_') and 'mode=memory' in configured,
            'isolated_database_required')
    else:
        need(type(selected) is str and configured == selected, 'isolated_database_required')
        path = Path(configured).resolve()
        sandbox = Path(__file__).resolve().parents[3] / 'temp'
        need(sandbox.resolve() in path.parents and path.suffix == '.sqlite3', 'isolated_database_required')


def account(user):
    isolated()
    need(user is not None and user.is_authenticated and user.pk is not None and user.is_active, 'login_required')
    return 'user:' + str(user.pk)


def _key(value):
    need(type(value) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value), 'request_key')


def _versions(body):
    for k in ['expected_revision', 'expected_epoch', 'expected_policy_version']:
        need(type(body[k]) is int and body[k] >= 0, 'expected_versions')


def _check(row, body):
    _versions(body)
    need(row.revision == body['expected_revision'], 'stale_state')
    need(row.epoch == body['expected_epoch'], 'stale_epoch')
    need(row.policy['version'] == body['expected_policy_version'], 'stale_policy')


def _clear(state):
    result = copy.deepcopy(state)
    result['pending'] = None
    result['revision'] += 1
    return decisions._seal(result)


@contextmanager
def _locked(user):
    owner = account(user)
    try:
        with transaction.atomic():
            # First SQL is a write reservation: SQLite select_for_update alone
            # cannot serialize writers. PostgreSQL also locks this owner row.
            need(NaturalMemoryLedger.objects.filter(user=user).update(lock_version=F('lock_version') + 1) == 1,
                'account_not_configured')
            row = NaturalMemoryLedger.objects.get(user=user)
            decisions._checked(row.state, row.policy, owner, row.revision)
            need(row.state['revision'] == row.revision, 'ledger_revision')
            yield row
    except (IntegrityError, OperationalError):
        raise NaturalMemoryContractError('write_conflict_retry_same_request') from None


def initialize_account(user):
    """Trusted fixture setup only; no creation from read or public settings API."""
    owner = account(user)
    policy = {'contract': decisions.POLICY, 'owner': owner, 'version': 1, 'collect_types': [],
        'use_enabled': False, 'preference_auto_enabled': False,
        'preference_auto_gate_passed': getattr(settings,'NATURAL_MEMORY_PREFERENCE_AUTO_RELEASED',False) is True}
    row, created = NaturalMemoryLedger.objects.get_or_create(user=user, defaults={
        'policy': policy, 'permissions': {'policy_version': 1, 'cloud_extraction': False, 'cloud_verification': False},
        'state': decisions.new_state(owner)})
    need(created, 'already_configured')
    return {'revision': row.revision, 'epoch': row.epoch, 'policy_version': 1}


def _invalidate_jobs(user, *, except_key=None):
    jobs = NaturalMemoryJob.objects.filter(user=user, status__in=['awaiting_extraction', 'awaiting_review'])
    if except_key is not None:
        jobs = jobs.exclude(job_key=except_key)
    jobs.update(status='stale', snapshot={}, extraction_reply='', reason='newer_account_state')
    from .natural_memory_lifecycle import invalidate_locked
    invalidate_locked(user)
    from .natural_memory_background import available, cancel_locked
    if available():
        cancel_locked(user, 'account_or_source_changed')


def configure(user, *, expected_epoch, expected_policy_version, collect_types, use_enabled,
              cloud_extraction, cloud_verification, preference_auto_enabled=False, preference_auto_gate_passed=False):
    """Synthetic trusted policy setup; not mounted as a user HTTP API."""
    with _locked(user) as row:
        need(type(expected_epoch) is int and row.epoch == expected_epoch, 'stale_epoch')
        need(type(expected_policy_version) is int and row.policy['version'] == expected_policy_version, 'stale_policy')
        policy = {**row.policy, 'version': row.policy['version'] + 1, 'collect_types': sorted(collect_types),
            'use_enabled': use_enabled, 'preference_auto_enabled': preference_auto_enabled,
            'preference_auto_gate_passed': preference_auto_gate_passed}
        decisions._checked(row.state, policy, account(user), row.revision)
        need(type(cloud_extraction) is bool and type(cloud_verification) is bool, 'processing_permission_type')
        row.policy = policy
        row.permissions = {'policy_version': policy['version'], 'cloud_extraction': cloud_extraction,
            'cloud_verification': cloud_verification}
        _persist(row, _clear(row.state)); row.epoch += 1; row.pending_epoch = None
        row.save(update_fields=['policy', 'permissions', 'epoch', 'pending_epoch'])
        _invalidate_jobs(user)
        return {'revision': row.revision, 'epoch': row.epoch, 'policy_version': policy['version']}


def _persist(row, state):
    need(state['revision'] > row.revision, 'nonadvancing_state')
    decisions._checked(state, row.policy, account(row.user), state['revision'])
    changed = NaturalMemoryLedger.objects.filter(pk=row.pk, revision=row.revision).update(
        state=state, revision=state['revision'])
    need(changed == 1, 'stale_state')
    row.state, row.revision = state, state['revision']


def _replay(user, key, fingerprint):
    old = NaturalMemoryOperation.objects.filter(user=user, request_key=key).first()
    if old is None:
        return None
    need(old.input_hash == fingerprint, 'request_key_reused')
    return {'status': 'replayed', 'receipt_revision': old.receipt['revision'],
        'receipt_epoch': old.receipt['epoch'], 'writes_performed': False, 'saved_ack': False}


def _operation_budget(user):
    if getattr(settings, 'NATURAL_MEMORY_BACKGROUND_ENABLED', False) is True:
        # Completed audit rows must not become a lifetime chat quota. Keep a
        # bounded number of live work tickets instead, without reviving old ones.
        tickets = list(NaturalMemoryOperation.objects.filter(user=user, request_key__startswith='r5-answer-',
            receipt__status__in=['answer_processing', 'answer_paused']).values_list('receipt', flat=True)[:129])
        need(len(tickets) <= 128, 'operation_budget')
        live = 0
        for ticket in tickets:
            try:
                live += timezone.datetime.fromisoformat(ticket['deadline_at']) > timezone.now()
            except (KeyError, TypeError, ValueError):
                live += 1
        need(live < 16, 'operation_budget')
    else:
        need(NaturalMemoryOperation.objects.filter(user=user).count() < 256, 'operation_budget')


def _record(row, key, fingerprint, status, *, saved=False, **metadata):
    _operation_budget(row.user)
    receipt = {'status': status, 'revision': row.revision, 'epoch': row.epoch,
        'writes_performed': True, 'saved_ack': saved, 'clinical_confirmation': False,
        'answer_status': 'not_scheduled_in_R4', **metadata}
    NaturalMemoryOperation.objects.create(user=row.user, request_key=key, input_hash=fingerprint, receipt=receipt)
    return receipt


def _conversation(user, cid):
    need(type(cid) is int and cid > 0, 'conversation_id')
    conv = Conversation.objects.select_for_update().filter(user=user, pk=cid).first()
    need(conv is not None, 'conversation_unavailable')
    return conv


def _envelope(message, user):
    return {'id': 'db:' + str(message.pk), 'owner': account(user),
        'session': 'conversation:' + str(message.conversation_id), 'role': message.role,
        'content': message.content, 'recorded_at': message.created_at.isoformat()}


def _sources(row, *snapshots, extra_ids=()):
    """Fetch only explicitly bound IDs, never enumerate a user's message history."""
    ids = set(extra_ids); sessions = set()
    for entry in row.state['entries'].values():
        for binding in entry['bindings']:
            sessions.add(binding['session']); ids.update(m['id'] for m in binding['messages'])
        if entry['confirmation']:
            ids.add(entry['confirmation']['source_id'])
    pending = row.state['pending']
    source_sets = list(snapshots) + ([pending['source']] if pending else [])
    for source in source_sets:
        sessions.add(source['session']); ids.update(m['id'] for m in source['messages'])
    need(len(ids) <= 512 and len(sessions) <= 64, 'source_recheck_budget')
    numeric = []
    for mid in ids:
        need(type(mid) is str and re.fullmatch(r'db:[1-9][0-9]*', mid), 'source_id')
        numeric.append(int(mid[3:]))
    messages = list(ChatMessage.objects.select_for_update().select_related('conversation').filter(
        pk__in=numeric, conversation__user=row.user))
    excluded = set(MemorySourceExclusion.objects.filter(user=row.user, message_id__in=numeric).values_list('message_id', flat=True))
    live = {}
    for message in messages:
        if message.pk > message.conversation.memory_after_id and message.pk not in excluded:
            live['db:' + str(message.pk)] = _envelope(message, row.user)
    versions = {}; latest = {}
    for session in sessions:
        need(type(session) is str and re.fullmatch(r'conversation:[1-9][0-9]*', session), 'session_id')
        conv = Conversation.objects.select_for_update().filter(pk=int(session.split(':')[1]), user=row.user).first()
        if conv is None:
            continue
        versions[session] = conv.memory_version
        last = ChatMessage.objects.filter(conversation=conv, role='user').order_by('-pk').first()
        if last is not None:
            mid = 'db:' + str(last.pk); latest[session] = mid
            if last.pk > conv.memory_after_id and last.pk not in excluded:
                live[mid] = _envelope(last, row.user)
    return {'live_sources': live, 'conversation_versions': versions, 'latest_user_ids': latest}


def _capture(row, conv, current, context_ids):
    need(type(context_ids) is list and len(context_ids) <= 4 and all(type(x) is int and x > 0 for x in context_ids)
        and len(set(context_ids)) == len(context_ids), 'context_ids')
    messages = list(ChatMessage.objects.filter(conversation=conv, pk__in=context_ids).order_by('pk'))
    need(len(messages) == len(context_ids), 'context_not_owned')
    excluded = ['db:' + str(x) for x in MemorySourceExclusion.objects.filter(user=row.user,
        message_id__in=context_ids + [current.pk]).values_list('message_id', flat=True)]
    return capture_context(_envelope(current, row.user), owner=account(row.user), session='conversation:' + str(conv.pk),
        policy_version=row.policy['version'], conversation_version=conv.memory_version,
        collect_types=row.policy['collect_types'], context=[_envelope(m, row.user) for m in messages],
        source_after_id=conv.memory_after_id, excluded_ids=sorted(set(excluded + row.state['blocked_sources'])))


def post_message(user, body):
    fields(body, {'request_key', 'conversation_id', 'content', 'context_ids', 'mode', 'display_digest',
        'expected_revision', 'expected_epoch', 'expected_policy_version', 'expected_conversation_version'}, 'message_input_fields')
    account(user); _key(body['request_key']); text(body['content'], 4096, 'message_budget')
    need(body['mode'] in {'new_message', 'clarification'}, 'message_mode')
    fingerprint = digest(['message', body])
    with _locked(user) as row:
        replay = _replay(user, body['request_key'], fingerprint)
        if replay is not None:
            return replay
        _check(row, body); conv = _conversation(user, body['conversation_id'])
        need(type(body['expected_conversation_version']) is int and conv.memory_version == body['expected_conversation_version'], 'stale_conversation')
        if body['mode'] == 'clarification':
            pending = row.state['pending']
            need(pending is not None and pending['unresolved'] and row.pending_epoch == row.epoch
                and pending['display_digest'] == body['display_digest'], 'clarification_display')
        else:
            need(body['display_digest'] is None, 'unsolicited_display')
        message = ChatMessage.objects.create(conversation=conv, role='user', content=body['content'])
        # Bind the representation that later ORM reads return. A configured
        # database timezone can differ from timezone.now() on the new object.
        message.refresh_from_db()
        source = _capture(row, conv, message, body['context_ids'])
        if body['mode'] == 'clarification':
            need({m['id'] for m in pending['source']['messages']} <= {m['id'] for m in source['messages']}, 'resolution_context_missing')
        else:
            _persist(row, _clear(row.state))
        row.epoch += 1
        row.save(update_fields=['epoch'])
        _invalidate_jobs(user)
        allowed = row.policy['collect_types'] and row.permissions['cloud_extraction']
        job = None
        if allowed:
            need(NaturalMemoryJob.objects.filter(user=user).count() < 128, 'job_budget')
            request = prepare_extraction(source, permissions=row.permissions, expected_policy_version=row.policy['version'],
                expected_conversation_version=conv.memory_version, expected_context_digest=source['context_digest'])
            job = NaturalMemoryJob.objects.create(user=user, job_key=uuid.uuid4().hex,
                source_message=message, epoch=row.epoch, revision=row.revision, mode=body['mode'], snapshot=source,
                deadline_at=timezone.now() + timedelta(seconds=90), audit={'context_digest': source['context_digest'],
                    'policy_version': source['policy_version'], 'conversation_version': source['conversation_version'],
                    'extraction_request_ref': request['request_ref'], 'extraction_prompt': request['prompt_version']})
        elif body['mode'] == 'clarification':
            _persist(row, _clear(row.state))
        return _record(row, body['request_key'], fingerprint, 'awaiting_extraction' if job else 'stored_without_collection',
            message_id=message.pk, job_key=job.job_key if job else None)


def _job(row, job_key):
    need(type(job_key) is str and re.fullmatch(r'[0-9a-f]{32}', job_key), 'job_key')
    job = NaturalMemoryJob.objects.select_for_update().filter(user=row.user, job_key=job_key).first()
    need(job is not None, 'job_unavailable')
    return job


def _remaining(job, request):
    left = (job.deadline_at - timezone.now()).total_seconds()
    need(left > 0, 'job_deadline')
    result = copy.deepcopy(request)
    result['remaining_original_seconds'] = left
    result['constraints']['timeout_seconds_cap'] = min(45, left)
    return result


def worker_request(user, job_key):
    """Internal-only request preparation, with no network execution."""
    with _locked(user) as row:
        job = _job(row, job_key)
        need(job.epoch == row.epoch and job.revision == row.revision, 'stale_job')
        need(timezone.now() < job.deadline_at, 'job_deadline')
        live = _sources(row, job.snapshot)
        decisions._source(job.snapshot, row.state, row.policy, **{k: live[k] for k in ['live_sources', 'conversation_versions']})
        need(live['latest_user_ids'].get(job.snapshot['session']) == job.snapshot['current_message_id'], 'newer_user_message')
        options = {'permissions': row.permissions, 'expected_policy_version': row.policy['version'],
            'expected_conversation_version': live['conversation_versions'][job.snapshot['session']],
            'expected_context_digest': job.snapshot['context_digest']}
        if job.status == 'awaiting_extraction':
            return _remaining(job, prepare_extraction(job.snapshot, **options))
        need(job.status == 'awaiting_review', 'job_closed')
        return _remaining(job, prepare_review(job.snapshot, job.extraction_reply, **options))


def submit_reply(user, job_key, *, stage, response, choices=None):
    """Trusted offline worker replies only; never accepts a client candidate/state."""
    account(user); need(stage in {'extraction', 'review'}, 'reply_stage')
    text(response, 8192, 'response_budget')
    fingerprint = digest(['worker', job_key, stage, response, choices])
    key = 'worker-' + job_key + '-' + stage
    with _locked(user) as row:
        job = _job(row, job_key)
        replay = _replay(user, key, fingerprint)
        if replay is not None:
            return replay
        need(job.status == ('awaiting_extraction' if stage == 'extraction' else 'awaiting_review'), 'job_closed')
        need(job.epoch == row.epoch and job.revision == row.revision, 'stale_job')
        need(timezone.now() < job.deadline_at, 'job_deadline')
        ex = response if stage == 'extraction' else job.extraction_reply
        rv = response if stage == 'review' else None
        live = _sources(row, job.snapshot)
        decisions._source(job.snapshot, row.state, row.policy, **{k: live[k] for k in ['live_sources', 'conversation_versions']})
        need(live['latest_user_ids'].get(job.snapshot['session']) == job.snapshot['current_message_id'], 'newer_user_message')
        result = preview(job.snapshot, permissions=row.permissions, expected_policy_version=row.policy['version'],
            expected_conversation_version=live['conversation_versions'][job.snapshot['session']],
            expected_context_digest=job.snapshot['context_digest'], extraction_response=ex, review_response=rv)
        if result['status'] == 'awaiting_review':
            need(stage == 'extraction' and choices is None, 'premature_choices')
            job.status = 'awaiting_review'; job.extraction_reply = response; job.extraction_hash = digest(response)
            job.audit.update(review_request_ref=result['request']['request_ref'],
                review_prompt=result['request']['prompt_version'])
            job.save(update_fields=['status', 'extraction_reply', 'extraction_hash', 'audit'])
            return _record(row, key, fingerprint, 'awaiting_review')
        if job.mode == 'clarification' and result['status'] in {'proposed', 'no_memory', 'pending_review'}:
            event = {'kind': 'reply', 'owner': account(user), 'session': job.snapshot['session'],
                'display_digest': row.state['pending']['display_digest'], 'decision': 'resolve',
                'message_id': job.snapshot['current_message_id']}
            decision = decisions.resolve(row.state, job.snapshot, owner=account(user), policy=row.policy,
                permissions=row.permissions, expected_revision=row.revision, request_key=key, event=event,
                extraction_response=ex, review_response=rv, choices=choices, **live)
        else:
            decision = decisions.propose(row.state, job.snapshot, policy=row.policy, permissions=row.permissions,
                expected_revision=row.revision, request_key=key, extraction_response=ex, review_response=rv, choices=choices, **live)
        _persist(row, decision['state'])
        row.epoch += 1; row.pending_epoch = row.epoch if row.state['pending'] else None
        row.save(update_fields=['epoch', 'pending_epoch'])
        from .natural_memory_lifecycle import invalidate_locked
        invalidate_locked(user)
        job.status = 'failed' if decision['status'] in {'processing_failed', 'blocked', 'needs_context'} else 'completed'
        job.reason = decision['status']; job.extraction_hash = digest(ex)
        job.review_hash = digest(rv) if rv is not None else ''
        job.audit['semantic_assurance'] = result.get('semantic_assurance')
        job.audit['review_outcomes'] = [{'item_ref': item['review']['item_ref'], 'verdict': bucket,
            'reason': item['review']['reason']} for bucket in ['supported', 'contradicted', 'unclear']
            for item in result.get(bucket, [])]
        job.audit['coverage'] = {k: result['coverage'][k] for k in ['verdict', 'reason']} if 'coverage' in result else None
        job.snapshot = {}; job.extraction_reply = ''
        job.save()
        saved = decision['status'] in {'projected_auto_preference', 'projected_confirmed_by_resolution'}
        return _record(row, key, fingerprint, decision['status'], saved=saved)


def confirm_display(user, body):
    fields(body, {'request_key', 'conversation_id', 'display_digest', 'decision', 'reply_content',
        'expected_revision', 'expected_epoch', 'expected_policy_version'}, 'confirmation_input_fields')
    account(user); _key(body['request_key']); fingerprint = digest(['confirm', body])
    with _locked(user) as row:
        replay = _replay(user, body['request_key'], fingerprint)
        if replay is not None:
            return replay
        _check(row, body); conv = _conversation(user, body['conversation_id'])
        need(row.pending_epoch == row.epoch and row.state['pending'] is not None, 'no_current_display')
        need(row.state['pending']['source']['session'] == 'conversation:' + str(conv.pk), 'intent_owner')
        reply = None
        if body['reply_content'] is not None:
            text(body['reply_content'], 4096, 'message_budget')
            reply = ChatMessage.objects.create(conversation=conv, role='user', content=body['reply_content'])
        live = _sources(row, extra_ids=['db:' + str(reply.pk)] if reply else [])
        event = {'kind': 'reply' if reply else 'click', 'owner': account(user), 'session': 'conversation:' + str(conv.pk),
            'display_digest': body['display_digest'], 'decision': body['decision'],
            'message_id': 'db:' + str(reply.pk) if reply else None}
        result = decisions.confirm(row.state, owner=account(user), policy=row.policy, expected_revision=row.revision,
            request_key=body['request_key'], event=event, **live)
        _persist(row, result['state']); row.epoch += 1; row.pending_epoch = None
        row.save(update_fields=['epoch', 'pending_epoch']); _invalidate_jobs(user)
        return _record(row, body['request_key'], fingerprint, result['status'], saved=body['decision'] == 'accept')


def manage_entry(user, body):
    fields(body, {'request_key', 'conversation_id', 'target', 'target_version', 'display_digest', 'action',
        'expected_revision', 'expected_epoch', 'expected_policy_version'}, 'management_input_fields')
    account(user); _key(body['request_key']); fingerprint = digest(['manage', body])
    with _locked(user) as row:
        replay = _replay(user, body['request_key'], fingerprint)
        if replay is not None:
            return replay
        _check(row, body); conv = _conversation(user, body['conversation_id'])
        live = _sources(row)
        session = 'conversation:' + str(conv.pk)
        # Management can be invoked from another owned conversation.
        latest = ChatMessage.objects.filter(conversation=conv, role='user').order_by('-pk').first()
        need(latest is not None, 'intent_latest_source')
        live['latest_user_ids'][session] = 'db:' + str(latest.pk)
        live['live_sources']['db:' + str(latest.pk)] = _envelope(latest, user)
        event = {'kind': 'click', 'owner': account(user), 'session': session, 'display_digest': body['display_digest'],
            'decision': body['action'], 'message_id': None}
        result = decisions.manage(row.state, owner=account(user), policy=row.policy, expected_revision=row.revision,
            request_key=body['request_key'], target=body['target'], target_version=body['target_version'],
            action=body['action'], event=event, **live)
        _persist(row, result['state']); row.epoch += 1; row.pending_epoch = None
        row.save(update_fields=['epoch', 'pending_epoch']); _invalidate_jobs(user)
        return _record(row, body['request_key'], fingerprint, result['status'])


def read_state(user):
    owner = account(user)
    # A read never initializes a policy, queues a job or writes a receipt.
    with transaction.atomic():
        row = NaturalMemoryLedger.objects.filter(user=user).first()
        if row is None:
            return {'status': 'unconfigured', 'writes_performed': False, 'entries': [], 'pending': None}
        decisions._checked(row.state, row.policy, owner, row.revision)
        live = _sources(row)
        eligible = {e['key'] for e in decisions.eligible_entries(row.state, owner=owner, policy=row.policy,
            **{k: live[k] for k in ['live_sources', 'conversation_versions']})}
        entries = [{'key': e['key'], 'version': e['version'], 'status': e['status'],
            'fact': copy.deepcopy(e['fact']), 'event_state': e['event_state'], 'user_confirmed': e['user_confirmed'],
            'clinical_confirmation': False, 'sources': copy.deepcopy(e['sources']),
            'display_digest': decisions.entry_display_digest(e), 'eligible': e['key'] in eligible}
            for e in row.state['entries'].values()]
        for shown in entries:
            try:
                decisions.preference_before_latest_change(row.state['entries'][shown['key']],row.state,
                    **{k:live[k] for k in ['live_sources','conversation_versions']})
                shown['can_undo']=True
            except NaturalMemoryContractError:shown['can_undo']=False
        pending = row.state['pending'] if row.pending_epoch == row.epoch else None
        if pending:
            try:
                need(pending['policy_hash'] == digest(row.policy), 'policy_changed')
                decisions._source(pending['source'], row.state, row.policy,
                    **{k: live[k] for k in ['live_sources', 'conversation_versions']})
                need(live['latest_user_ids'].get(pending['source']['session']) == pending['source']['current_message_id'], 'newer_user_message')
            except NaturalMemoryContractError:
                pending = None
        display = None
        if pending:
            display = {k: copy.deepcopy(pending[k]) for k in ['display_digest', 'unresolved', 'questions', 'clarification_facts']}
            # R2 wraps each question with its review. The public display needs
            # the question object itself, whose `question` field is plain text.
            display['questions'] = [copy.deepcopy(q['question']) for q in pending['questions']]
            display['actions'] = [{k: copy.deepcopy(a[k]) for k in ['operation', 'target', 'target_version',
                'fact', 'previous_fact', 'event_state', 'semantic_assurance', 'sources']} for a in pending['actions']]
        # Verify the read version before returning a display; a future write
        # must still perform its own atomic recheck, not trust this response.
        need(NaturalMemoryLedger.objects.filter(pk=row.pk, revision=row.revision, epoch=row.epoch).exists(), 'read_changed')
        return {'status': 'ready', 'revision': row.revision, 'epoch': row.epoch,
            'policy_version': row.policy['version'], 'entries': entries, 'pending': display, 'writes_performed': False}
