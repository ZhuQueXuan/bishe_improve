"""P03 internal new-message preview and gated local save; no chat/API caller.

Never accepts candidate text from the UI/model at commit. Rebuild the preview
from the owned current DB message and compare its digest, full hash, permissions
and reset version. Existing attributes require P04 instead of blind insertion.
"""
import re
from django.db import transaction
from django.db.models import F
from .models import ChatMessage, PersonalMemoryEntry, MemorySourceExclusion
from .personal_memory_store import account, need, read_policy, commit_candidate
from .personal_memory_extraction import prepare_request, extract_preview


def preview_new_message(user, message_id, *, conversation_id, expected_policy_version, expected_conversation_version):
    owner = account(user)
    need(type(message_id) is int and message_id > 0 and type(conversation_id) is int and conversation_id > 0, 'source_id_required')
    policy = read_policy(user)
    need(type(expected_policy_version) is int and policy['version'] == expected_policy_version, 'stale_policy')
    msg = ChatMessage.objects.select_related('conversation').filter(pk=message_id, conversation_id=conversation_id,
        conversation__user=user, role='user').first()
    need(msg is not None, 'source_unavailable')
    conv = msg.conversation
    need(type(expected_conversation_version) is int and conv.memory_version == expected_conversation_version, 'source_conversation_changed')
    need(msg.pk > conv.memory_after_id and not MemorySourceExclusion.objects.filter(user=user, message_id=msg.pk).exists(), 'source_excluded_or_reset')
    # A new-message adapter cannot silently scan older user messages. History
    # ingestion will have a different authorized entry point in P07.
    need(not ChatMessage.objects.filter(conversation=conv, role='user', pk__gt=msg.pk).exists(), 'not_latest_user_message')
    envelope = {'id': f'db:{msg.pk}', 'owner': owner, 'session': f'conversation:{conv.pk}', 'role': msg.role,
        'content': msg.content, 'recorded_at': msg.created_at.isoformat()}
    preview = extract_preview(prepare_request(envelope, owner=owner, session=envelope['session'], allowed_types=policy['collect_types']))
    return {**preview, 'policy_version': policy['version'], 'conversation_version': conv.memory_version}


def save_new_preview(user, message_id, *, conversation_id, expected_policy_version, expected_conversation_version,
                     expected_source_hash, expected_preview_digest, request_key):
    need(isinstance(request_key, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,40}', request_key), 'invalid_request_key')
    need(isinstance(expected_source_hash, str) and re.fullmatch(r'[0-9a-f]{64}', expected_source_hash), 'source_hash_required')
    with transaction.atomic():
        preview = preview_new_message(user, message_id, conversation_id=conversation_id,
            expected_policy_version=expected_policy_version, expected_conversation_version=expected_conversation_version)
        need(preview['source_hash'] == expected_source_hash, 'source_changed')
        need(preview['preview_digest'] == expected_preview_digest, 'preview_changed')
        receipts = []
        for index, candidate in enumerate(preview['candidates']):
            r = candidate['record']
            # Do not treat a denial, repeated condition or new default as an
            # independent active fact beside an existing attribute. P04 resolves
            # targets/events/conflicts. Exact replay uses the same deterministic ID.
            existing = PersonalMemoryEntry.objects.filter(user=user, revisions__version=F('version'),
                revisions__candidate__record__attribute=r['attribute']).exclude(memory_key=r['id']).exists()
            need(not existing, 'existing_attribute_requires_p04')
            receipt = commit_candidate(user, candidate, request_key='p03-' + request_key + '-' + str(index),
                expected_policy_version=expected_policy_version, source_conversation_version=expected_conversation_version,
                expected_source_hash=expected_source_hash)
            need(receipt['status'] in {'committed', 'replayed'}, 'candidate_not_committed')
            receipts.append(receipt)
        return {'status': 'committed' if receipts else 'nothing_saved', 'receipts': receipts,
            'writes_performed': any(r['writes_performed'] for r in receipts), 'user_confirmed': False, 'clinical_confirmation': False}
