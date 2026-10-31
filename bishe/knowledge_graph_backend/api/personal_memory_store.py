"""P02 transactional storage primitives; no chat callers, extraction or workers.

P03 supplies semantically supported candidates. This adapter independently checks
owned DB sources, reset/exclusion state, explicit policy and target versions.
New tables are not initialized or queried on import, and reads never create rows.
"""
import copy
import hashlib
import json
import re
from django.db import transaction, IntegrityError, OperationalError
from django.db.models import F
from django.utils import timezone
from .models import (ChatMessage, Conversation, MemorySourceExclusion, PersonalMemoryPolicy,
                     PersonalMemoryEntry, PersonalMemoryRevision, PersonalMemoryReceipt, PersonalMemoryIndexTask)
from .memory_contract import MemoryContractError
from .personal_memory_contract import TYPE_RULES, validate_candidate, decide_personal_update


class PersonalMemoryStoreError(ValueError):
    """Static codes only: no SQL text, private payload or credentials."""


def need(value, code):
    if not value:
        raise PersonalMemoryStoreError(code)


def fingerprint(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise PersonalMemoryStoreError('invalid_json') from None
    need(len(raw) <= 24576, 'input_too_large')
    return hashlib.sha256(raw).hexdigest()


def account(user):
    need(user is not None and user.is_authenticated and user.pk is not None, 'login_required')
    return f'user:{user.pk}'


def key(value):
    need(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value), 'opaque_memory_key_required')
    return value


def read_policy(user):
    account(user)
    row = PersonalMemoryPolicy.objects.filter(user=user).first()
    return {'version': row.version if row else 0, 'collect_types': list(row.collect_types) if row else [],
            'history_conversation_ids': list(row.history_conversation_ids) if row else [],
            'use_enabled': row.use_enabled if row else False}


def set_policy(user, *, expected_version, collect_types, history_conversation_ids, use_enabled):
    account(user)
    need(type(expected_version) is int and expected_version >= 0, 'policy_version_required')
    need(type(use_enabled) is bool, 'use_flag_required')
    need(isinstance(collect_types, list) and len(collect_types) <= len(TYPE_RULES) and
         all(isinstance(t, str) and t in TYPE_RULES for t in collect_types) and len(set(collect_types)) == len(collect_types), 'invalid_types')
    need(isinstance(history_conversation_ids, list) and len(history_conversation_ids) <= 50 and
         all(type(cid) is int and cid > 0 for cid in history_conversation_ids) and
         len(set(history_conversation_ids)) == len(history_conversation_ids), 'invalid_history_selection')
    try:
        with transaction.atomic():
            row, _ = PersonalMemoryPolicy.objects.get_or_create(user=user)
            need(Conversation.objects.filter(user=user, pk__in=history_conversation_ids).count() == len(history_conversation_ids), 'history_not_owned')
            changed = PersonalMemoryPolicy.objects.filter(pk=row.pk, version=expected_version).update(
                version=F('version')+1, collect_types=sorted(collect_types),
                history_conversation_ids=sorted(history_conversation_ids), use_enabled=use_enabled, updated_at=timezone.now())
            need(changed == 1, 'stale_policy')
            PersonalMemoryIndexTask.objects.filter(entry__user=user, status='queued').update(status='stale')
        return read_policy(user)
    except (IntegrityError, OperationalError):
        raise PersonalMemoryStoreError('write_conflict_retry_same_request') from None


def _source(candidate, user, expected_conversation_version=None):
    need(isinstance(candidate, dict) and isinstance(candidate.get('record'), dict), 'invalid_candidate')
    r = candidate['record']
    need(r.get('owner') == account(user), 'source_not_owned')
    source = r.get('source')
    need(isinstance(source, dict), 'invalid_source')
    mid = source.get('message_id')
    need(isinstance(mid, str) and re.fullmatch(r'db:[1-9][0-9]*', mid), 'database_source_required')
    msg = ChatMessage.objects.select_related('conversation').filter(pk=int(mid[3:]), conversation__user=user, role='user').first()
    need(msg is not None, 'source_unavailable')
    conv = msg.conversation
    need(type(expected_conversation_version) is int and conv.memory_version == expected_conversation_version, 'source_conversation_changed')
    need(msg.pk > conv.memory_after_id and not MemorySourceExclusion.objects.filter(user=user, message_id=msg.pk).exists(), 'source_excluded_or_reset')
    messages = {mid: {'id': mid, 'owner': account(user), 'session': f'conversation:{conv.pk}',
                      'role': msg.role, 'content': msg.content, 'recorded_at': msg.created_at.isoformat()}}
    try:
        checked = validate_candidate(candidate, messages)
    except MemoryContractError:
        raise PersonalMemoryStoreError('candidate_source_invalid') from None
    key(checked['record']['id'])
    return checked, msg, messages


def _checked_revision(revision, user):
    need(revision.entry.user_id == user.pk and revision.status != 'forgotten', 'revision_unavailable')
    candidate, msg, messages = _source(revision.candidate, user, revision.conversation_version)
    need(revision.source_message_id == msg.pk and revision.source_hash == hashlib.sha256(msg.content.encode()).hexdigest()
         and revision.source_time == msg.created_at, 'source_changed')
    return candidate, messages


def read_current(user, memory_key):
    account(user); key(memory_key)
    policy = PersonalMemoryPolicy.objects.filter(user=user, use_enabled=True).first()
    if policy is None:
        return None
    entry = PersonalMemoryEntry.objects.filter(user=user, memory_key=memory_key, status='active').first()
    if entry is None:
        return None
    revision = PersonalMemoryRevision.objects.filter(entry=entry, version=entry.version, status='active').select_related('entry').first()
    if revision is None:
        return None
    try:
        candidate, _ = _checked_revision(revision, user)
    except PersonalMemoryStoreError:
        return None
    if not (PersonalMemoryPolicy.objects.filter(pk=policy.pk, version=policy.version, use_enabled=True).exists() and
            PersonalMemoryEntry.objects.filter(pk=entry.pk, user=user, version=entry.version, status='active').exists()):
        return None
    return {'memory_key': entry.memory_key, 'version': entry.version, 'policy_version': policy.version,
            'candidate': candidate, 'origin': revision.origin, 'user_confirmed': revision.user_confirmed,
            'index_status': revision.index_status}


def read_history(user, memory_key):
    """Metadata only, never restores old plaintext as current memory."""
    account(user); key(memory_key)
    return list(PersonalMemoryRevision.objects.filter(entry__user=user, entry__memory_key=memory_key).order_by('version').values(
        'version', 'previous_version', 'status', 'origin', 'user_confirmed', 'index_status', 'created_at'))


def _enqueue(entry, revision, operation, policy_version):
    return PersonalMemoryIndexTask.objects.create(entry=entry, revision=revision, operation=operation,
        policy_version=policy_version, content_hash=fingerprint(revision.candidate))


def commit_candidate(user, candidate, *, request_key, expected_policy_version, source_conversation_version,
                     expected_source_hash, target_key=None, expected_entry_version=None, intent='new_information', source_origin='current'):
    account(user)
    need(isinstance(request_key, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_key), 'invalid_request_key')
    need(type(expected_policy_version) is int and expected_policy_version >= 1, 'policy_version_required')
    need(isinstance(expected_source_hash, str) and re.fullmatch(r'[0-9a-f]{64}', expected_source_hash), 'source_hash_required')
    request_hash = fingerprint({'candidate': candidate, 'policy': expected_policy_version, 'target': target_key,
        'entry_version': expected_entry_version, 'source_version': source_conversation_version, 'source_hash': expected_source_hash,
        'intent': intent, 'origin': source_origin})
    try:
        with transaction.atomic():
            # This conditional write acquires SQLite's writer lock before reads.
            # select_for_update alone does not lock SQLite rows. Other databases
            # also lock this policy row; entry CAS below remains mandatory.
            locked = PersonalMemoryPolicy.objects.filter(user=user, version=expected_policy_version).update(version=F('version'))
            need(locked == 1, 'stale_or_missing_policy')
            policy = PersonalMemoryPolicy.objects.get(user=user)
            prior_receipt = PersonalMemoryReceipt.objects.filter(user=user, request_key=request_key).first()
            if prior_receipt:
                need(prior_receipt.input_hash == request_hash, 'request_key_conflict')
                # Never reapply old writes or return stored personal text.
                return {**prior_receipt.result, 'status': 'replayed', 'writes_performed': False,
                        'memory_changed': False, 'currently_usable': False}
            new, msg, messages = _source(candidate, user, source_conversation_version)
            need(hashlib.sha256(msg.content.encode()).hexdigest() == expected_source_hash, 'source_changed')
            entry = None; previous = None; old = None
            lifecycle = intent in {'forget', 'pause'}
            if target_key is not None:
                key(target_key)
                need(type(expected_entry_version) is int and expected_entry_version >= 1, 'entry_version_required')
                entry = PersonalMemoryEntry.objects.filter(user=user, memory_key=target_key).first()
                statuses = {'active','paused','disputed'} if intent=='forget' else {'active'}
                need(entry is not None and entry.version == expected_entry_version and entry.status in statuses, 'stale_or_unavailable_target')
                previous = PersonalMemoryRevision.objects.select_related('entry').filter(entry=entry, version=entry.version).first()
                need(previous is not None, 'target_revision_unavailable')
                if lifecycle:
                    # An explicit owned-target disable/forget must remain possible
                    # when the old source disappeared. It cannot authorize new
                    # clinical content, so do not fabricate valid old evidence.
                    need(new['record']['source']['mode']=='explicit_request' and source_origin=='current' and
                         new['record']['subject']=='self' and new['record']['scope']=='long_term' and
                         new['record']['storage_state']=='active', 'explicit_lifecycle_source_required')
                    old = copy.deepcopy(previous.candidate)
                    if isinstance(old,dict) and isinstance(old.get('record'),dict):
                        need((old.get('category'),old['record'].get('attribute')) ==
                             (new['category'],new['record']['attribute']), 'lifecycle_target_mismatch')
                else:
                    old, old_messages = _checked_revision(previous, user); messages.update(old_messages)
            else:
                need(expected_entry_version is None, 'unexpected_entry_version')
            if lifecycle and entry is not None:
                decision={'action':intent}
            else:
                try:
                    decision = decide_personal_update(new, messages, owner=account(user), allowed_types=policy.collect_types,
                        old=old, intent=intent, source_origin=source_origin,
                        history_sessions=[f'conversation:{cid}' for cid in policy.history_conversation_ids],
                        target_id=old['record']['id'] if old else None,
                        expected_revision=old['record']['revision'] if old else None)
                except MemoryContractError:
                    raise PersonalMemoryStoreError('candidate_policy_invalid') from None
            action = decision['action']
            if action in {'reject', 'clarify', 'temporary_only'}:
                return {'status': action, 'reason': decision['reason'], 'writes_performed': False}
            if action in {'add', 'keep_separate'}:
                need(not PersonalMemoryEntry.objects.filter(user=user, memory_key=new['record']['id']).exists(), 'memory_key_exists')
                entry = PersonalMemoryEntry.objects.create(user=user, memory_key=new['record']['id'])
                previous = None
            elif action != 'no_change':
                changed = PersonalMemoryEntry.objects.filter(pk=entry.pk, user=user, version=expected_entry_version, status=entry.status).update(
                    version=F('version')+1, status={'forget':'forgotten','pause':'paused'}.get(action,'active'), updated_at=timezone.now())
                need(changed == 1, 'stale_or_unavailable_target')
                entry.refresh_from_db()
                PersonalMemoryRevision.objects.filter(entry=entry, status='active').update(status='superseded')
                PersonalMemoryIndexTask.objects.filter(entry=entry, status='queued').update(status='stale')
            if action != 'no_change':
                payload = copy.deepcopy(new)
                if action == 'forget':
                    mids = set(PersonalMemoryRevision.objects.filter(entry=entry).exclude(source_message_id=None).values_list('source_message_id',flat=True))
                    mids.update(PersonalMemoryReceipt.objects.filter(user=user,entry=entry).exclude(source_message_id=None).values_list('source_message_id',flat=True))
                    mids.add(msg.pk)
                    for mid in mids:
                        MemorySourceExclusion.objects.get_or_create(user=user,message_id=mid)
                    PersonalMemoryRevision.objects.filter(entry=entry).update(candidate={},source_hash='',status='forgotten')
                    payload = {}
                payload_record = payload.get('record')
                if payload_record:
                    payload_record['revision'] = entry.version
                revision = PersonalMemoryRevision.objects.create(entry=entry, version=entry.version,
                    previous_version=previous.version if previous else None, candidate=payload,
                    source_message=None if action=='forget' else msg, source_hash='' if action=='forget' else hashlib.sha256(msg.content.encode()).hexdigest(),
                    conversation_version=msg.conversation.memory_version, source_time=msg.created_at,
                    origin='automatic_history' if source_origin=='history' else 'automatic_candidate',
                    status=entry.status, index_status='pending_delete' if action in {'pause','forget'} else 'pending')
                _enqueue(entry, revision, 'delete' if action in {'pause','forget'} else 'upsert', policy.version)
            result={'status':'committed','action':action,'memory_key':entry.memory_key,'version':entry.version,
                    'writes_performed':True,'memory_changed':action!='no_change','user_confirmed':False,'clinical_confirmation':False}
            PersonalMemoryReceipt.objects.create(user=user,request_key=request_key,input_hash=request_hash,result=result,
                                                 entry=entry,source_message=msg)
            return result
    except (IntegrityError, OperationalError):
        raise PersonalMemoryStoreError('write_conflict_retry_same_request') from None
