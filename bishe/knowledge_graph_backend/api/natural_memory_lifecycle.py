"""R6 isolated derivative fences, atomic erasure and one-shot continuations.

Bindings are local drift detectors, never authentication signatures. Public
access still authenticates the owner; only trusted code may create payloads.
"""
import copy
import re
import uuid
from django.db.models import Max
from django.utils import timezone
from .models import ChatMessage
from .natural_memory_models import NaturalMemoryDerived, NaturalMemoryOperation, NaturalMemoryAnswerGrant
from .natural_memory_contract import NaturalMemoryContractError, fields, need, digest, wire
from . import natural_memory_intake as intake

KINDS = {'cache', 'summary', 'checkpoint', 'answer_view'}


def _grants(user):
    row = NaturalMemoryAnswerGrant.objects.filter(user=user).first()
    return None if row is None else {'version': row.version, 'cloud_routing': row.cloud_routing,
        'cloud_answer_personal_context': row.cloud_answer_personal_context}


def _frame(ledger, source):
    live = intake._sources(ledger, source)
    return {'owner': intake.account(ledger.user), 'revision': ledger.revision, 'epoch': ledger.epoch,
        'state_seal': ledger.state['seal'], 'policy_hash': digest(ledger.policy),
        'processing_permissions_hash': digest(ledger.permissions), 'answer_grants_hash': digest(_grants(ledger.user)),
        'session': source['session'], 'query_ids': [m['id'] for m in source['messages']],
        'sources': {key: digest(value) for key, value in live['live_sources'].items()},
        'conversation_versions': live['conversation_versions'], 'latest_user_ids': live['latest_user_ids']}


def _seal(kind, binding, payload_hash, expires):
    return digest(['natural-memory-derived-v1', kind, binding, payload_hash, expires.isoformat() if expires else None])


def save_locked(ledger, kind, payload, source, *, expires=None):
    need(kind in KINDS, 'derived_kind')
    need(len(wire(payload)) <= 131072, 'derived_payload_budget')
    need(NaturalMemoryDerived.objects.filter(user=ledger.user).count() < 128, 'derived_count_budget')
    binding = _frame(ledger, source); hashed = digest(payload)
    row = NaturalMemoryDerived.objects.create(user=ledger.user, artifact_key=uuid.uuid4().hex, kind=kind,
        binding=binding, payload=copy.deepcopy(payload), payload_hash=hashed, expires_at=expires,
        seal=_seal(kind, binding, hashed, expires))
    return row.artifact_key


def invalidate_locked(user, *, except_ticket=None, reason='account_changed'):
    """Called in the same owner transaction as the authoritative version change."""
    NaturalMemoryDerived.objects.filter(user=user, status='ready').update(status='stale', payload={}, reason=reason)
    tickets = NaturalMemoryOperation.objects.filter(user=user, request_key__startswith='r5-answer-')
    if except_ticket:
        tickets = tickets.exclude(request_key=except_ticket)
    for operation in tickets:
        data = operation.receipt
        if data.get('status') in {'answer_processing', 'answer_paused', 'answered'}:
            data.update(status='stale', reason=reason)
            # Tickets have no prompt copies. Remove any continuation/display
            # handles too, while retaining hashes and spent-call audit counts.
            data.pop('checkpoint_key', None); data.pop('delivery_key', None)
            operation.receipt = data; operation.save(update_fields=['receipt'])


def _check(row, ledger):
    need(row.status == 'ready', 'derived_' + row.status)
    need(row.payload_hash == digest(row.payload), 'derived_payload_changed')
    need(row.seal == _seal(row.kind, row.binding, row.payload_hash, row.expires_at), 'derived_binding_changed')
    need(row.expires_at is None or timezone.now() < row.expires_at, 'derived_expired')
    # Only explicit bound IDs are reloaded; no chat-history scan or model read.
    stub = {'session': row.binding['session'], 'messages': [{'id': mid} for mid in row.binding['query_ids']]}
    need(_frame(ledger, stub) == row.binding, 'derived_context_changed')


def load_locked(ledger, key, *, kind=None, consume=False):
    need(type(key) is str and re.fullmatch(r'[0-9a-f]{32}', key), 'derived_key')
    row = NaturalMemoryDerived.objects.select_for_update().filter(user=ledger.user, artifact_key=key).first()
    if row is None:
        return {'status': 'unavailable', 'reason': 'derived_unavailable', 'payload': None, 'writes_performed': False}
    if kind is not None and row.kind != kind:
        return {'status': 'unavailable', 'reason': 'derived_kind', 'payload': None, 'writes_performed': False}
    try:
        _check(row, ledger)
    except NaturalMemoryContractError as error:
        changed = bool(row.payload) or row.status == 'ready'
        if changed:
            row.payload = {}; row.status = 'stale'; row.reason = str(error)
            row.save(update_fields=['payload', 'status', 'reason'])
        return {'status': 'unavailable', 'reason': str(error), 'payload': None, 'writes_performed': changed}
    payload = copy.deepcopy(row.payload)
    if consume:
        need(NaturalMemoryDerived.objects.filter(pk=row.pk, status='ready').update(status='consumed', payload={}) == 1,
            'derived_already_claimed')
    return {'status': 'ready', 'payload': payload, 'binding': copy.deepcopy(row.binding),
        'expires_at': row.expires_at, 'writes_performed': consume}


def read_derived(user, key, *, kind):
    with intake._locked(user) as ledger:
        return load_locked(ledger, key, kind=kind)


def delivery_locked(ledger, key):
    result = load_locked(ledger, key, kind='answer_view')
    if result['status'] != 'ready':
        return {'status': 'answer_unavailable', 'reason': result['reason'], 'answer': None, 'saved_ack': False}
    binding = result['binding']; permission = _grants(ledger.user)
    return {**result['payload'], 'delivery_key': key, 'fence': {'owner': binding['owner'],
        'session': binding['session'], 'revision': ledger.revision, 'epoch': ledger.epoch,
        'policy_version': ledger.policy['version'], 'answer_grant_version': permission['version'] if permission else 0}}


def read_delivery(user, key):
    with intake._locked(user) as ledger:
        return delivery_locked(ledger, key)


def checkpoint_locked(ledger, *, source, ticket_key, ticket, intent_raw, evidence, model_request):
    need(ticket['routing_calls'] == 1 and ticket['answer_calls'] == 0, 'checkpoint_call_budget')
    return save_locked(ledger, 'checkpoint', {'contract': 'natural-memory-continuation-v1',
        'ticket_key': ticket_key, 'source': source, 'ticket': ticket,
        'intent_raw': intent_raw, 'evidence': evidence, 'request': model_request}, source,
        expires=timezone.datetime.fromisoformat(ticket['deadline_at']))


def resume(user, body, *, providers=None):
    fields(body, {'checkpoint_key', 'expected_revision', 'expected_epoch'}, 'resume_fields')
    with intake._locked(user) as ledger:
        result = load_locked(ledger, body['checkpoint_key'], kind='checkpoint')
        if result['status'] != 'ready':
            return {'status': 'answer_unavailable', 'reason': result['reason'], 'answer': None, 'saved_ack': False}
        need(type(body['expected_revision']) is int and body['expected_revision'] == ledger.revision
            and type(body['expected_epoch']) is int and body['expected_epoch'] == ledger.epoch, 'stale_resume')
        payload = result['payload']
        need(payload['contract'] == 'natural-memory-continuation-v1', 'checkpoint_contract')
        op = NaturalMemoryOperation.objects.select_for_update().get(user=user, request_key=payload['ticket_key'])
        ticket = op.receipt
        need(ticket['status'] == 'answer_paused' and ticket['routing_calls'] == 1 and ticket['answer_calls'] == 0,
            'checkpoint_call_budget')
        # Validate and claim before releasing the write lock; the claim and
        # phase change survive a crash without granting a new attempt/deadline.
        from .natural_memory_answering import _current
        _current(ledger, ticket, payload['source'])
        load_locked(ledger, body['checkpoint_key'], kind='checkpoint', consume=True)
        ticket.update(status='answer_processing', phase='resuming')
        op.receipt = ticket; op.save(update_fields=['receipt'])
    from django.conf import settings
    from .natural_memory_answering import continue_answer
    if providers is None:
        providers = getattr(settings, 'NATURAL_MEMORY_FIXTURE_PROVIDERS', None)
    return continue_answer(user, payload, providers=providers)


def reset_conversation(user, body):
    fields(body, {'request_key', 'conversation_id', 'expected_revision', 'expected_epoch',
        'expected_policy_version', 'expected_conversation_version'}, 'reset_fields')
    intake._key(body['request_key']); fingerprint = digest(['reset', body])
    with intake._locked(user) as ledger:
        replay = intake._replay(user, body['request_key'], fingerprint)
        if replay is not None:
            return replay
        intake._check(ledger, body); conv = intake._conversation(user, body['conversation_id'])
        need(type(body['expected_conversation_version']) is int and conv.memory_version == body['expected_conversation_version'],
            'stale_conversation')
        conv.memory_after_id = ChatMessage.objects.filter(conversation=conv).aggregate(value=Max('pk'))['value'] or 0
        conv.memory_version += 1; conv.save(update_fields=['memory_after_id', 'memory_version'])
        intake._persist(ledger, intake._clear(ledger.state)); ledger.epoch += 1; ledger.pending_epoch = None
        ledger.save(update_fields=['epoch', 'pending_epoch']); intake._invalidate_jobs(user)
        return intake._record(ledger, body['request_key'], fingerprint, 'conversation_reset',
            conversation_version=conv.memory_version)
