"""R1 pure source and shape contract. No extraction, model, database or writes.

All caller envelopes/versions/grants must come from a trusted server adapter.
Hashes bind snapshots, not authority. A located quote does not establish its
subject, negation, certainty or scope. Every output still needs R2 semantics
and R3/R4 authorization; it is never a legacy/P02 writable candidate.
"""
import copy
from datetime import datetime
import hashlib
import json
import re

SOURCE_CONTRACT = 'natural-memory-source-v1'
CANDIDATE_CONTRACT = 'natural-memory-candidate-v1'
RESPONSE_CONTRACT = 'natural-memory-proposals-v1'
GROUNDED_CONTRACT = 'natural-memory-grounded-v1'
TYPES = frozenset({'self_disease_report', 'presentation_preference'})
PREFERENCES = {'style': frozenset({'plain', 'technical'}), 'length': frozenset({'brief', 'detailed'})}
POLARITIES = frozenset({'affirmed', 'negated', 'unknown'})
CERTAINTIES = frozenset({'asserted', 'possible', 'unknown'})
RELATIONS = frozenset({'new_information', 'supplement', 'same_event_update', 'new_event',
    'correction', 'new_default', 'retraction', 'unresolved'})
MAX_CURRENT_BYTES = 4096
MAX_ADJACENT_BYTES = 4096
MAX_ADJACENT_MESSAGES = 4
MAX_REQUEST_BYTES = 32768
MAX_RESPONSE_BYTES = 8192
MAX_ITEMS = 5
MESSAGE_FIELDS = {'id', 'owner', 'session', 'role', 'content', 'recorded_at'}
SNAPSHOT_FIELDS = {'contract', 'owner', 'session', 'policy_version', 'conversation_version',
    'collect_types', 'source_after_id', 'excluded_ids', 'context_complete', 'messages',
    'current_message_id', 'context_digest'}


class NaturalMemoryContractError(ValueError):
    """Static error code only; no user text or provider exceptions."""


def need(condition, code):
    if not condition:
        raise NaturalMemoryContractError(code)


def fields(value, expected, code):
    need(type(value) is dict and set(value) == expected, code)


def text(value, maximum, code):
    need(type(value) is str and bool(value.strip()), code)
    try:
        need(len(value.encode('utf-8')) <= maximum, code)
    except UnicodeError:
        raise NaturalMemoryContractError('invalid_unicode') from None
    return value


def wire(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
            allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise NaturalMemoryContractError('invalid_json_value') from None


def digest(value):
    return hashlib.sha256(wire(value)).hexdigest()


def content_hash(value):
    text(value, MAX_CURRENT_BYTES, 'message_budget')
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def stamp(value):
    text(value, 80, 'source_time')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        need(parsed.tzinfo is not None and parsed.utcoffset() is not None, 'source_time')
        return parsed
    except (ValueError, OverflowError):
        raise NaturalMemoryContractError('source_time') from None


def db_id(value):
    need(type(value) is str and re.fullmatch(r'db:[1-9][0-9]{0,18}', value), 'source_id')
    return int(value[3:])


def capture_context(current, *, owner, session, policy_version, conversation_version, collect_types,
                    context=(), source_after_id=0, excluded_ids=(), context_complete=True):
    """Bind full messages and versions; do not truncate or infer permissions."""
    need(type(owner) is str and re.fullmatch(r'user:[1-9][0-9]{0,18}', owner), 'login_required')
    need(type(session) is str and re.fullmatch(r'conversation:[1-9][0-9]{0,18}', session), 'session_required')
    need(type(policy_version) is int and policy_version >= 0, 'policy_version')
    need(type(conversation_version) is int and conversation_version >= 0, 'conversation_version')
    need(type(source_after_id) is int and source_after_id >= 0, 'reset_cursor')
    need(type(context_complete) is bool, 'context_complete')
    need(type(collect_types) in (list, tuple) and len(collect_types) <= len(TYPES) and all(type(t) is str and t in TYPES for t in collect_types)
        and len(set(collect_types)) == len(collect_types), 'collection_types')
    need(type(context) in (list, tuple) and len(context) <= MAX_ADJACENT_MESSAGES, 'context_count')
    need(type(excluded_ids) in (list, tuple) and len(excluded_ids) <= 2000, 'excluded_sources')
    for mid in excluded_ids:
        db_id(mid)
    fields(current, MESSAGE_FIELDS, 'message_fields')
    need(current['role'] == 'user', 'current_not_user')
    current_id = db_id(current['id'])
    current_time = stamp(current['recorded_at'])
    messages = []
    last_id = source_after_id
    last_time = None
    adjacent_size = 0
    for index, msg in enumerate([*context, current]):
        fields(msg, MESSAGE_FIELDS, 'message_fields')
        need(msg['owner'] == owner and msg['session'] == session, 'source_partition')
        need(type(msg['role']) is str and msg['role'] in {'user', 'assistant'}, 'source_role')
        mid = db_id(msg['id']); when = stamp(msg['recorded_at'])
        need(mid > last_id and mid <= current_id and when <= current_time and
             (last_time is None or when >= last_time), 'source_order_or_reset')
        need(msg['id'] not in excluded_ids, 'source_excluded')
        text(msg['content'], MAX_CURRENT_BYTES, 'message_budget')
        if index < len(context):
            adjacent_size += len(msg['content'].encode('utf-8'))
        messages.append({**copy.deepcopy(msg), 'message_hash': content_hash(msg['content'])})
        last_id, last_time = mid, when
    need(adjacent_size <= MAX_ADJACENT_BYTES, 'context_budget')
    snapshot = {'contract': SOURCE_CONTRACT, 'owner': owner, 'session': session,
        'policy_version': policy_version, 'conversation_version': conversation_version,
        'collect_types': sorted(collect_types), 'source_after_id': source_after_id,
        'excluded_ids': sorted(set(excluded_ids)), 'context_complete': context_complete,
        'messages': messages, 'current_message_id': current['id']}
    return {**snapshot, 'context_digest': digest(snapshot)}


def checked_context(snapshot, *, expected_policy_version, expected_conversation_version, expected_context_digest):
    fields(snapshot, SNAPSHOT_FIELDS, 'snapshot_fields')
    need(snapshot['contract'] == SOURCE_CONTRACT, 'source_contract')
    need(type(expected_policy_version) is int and snapshot['policy_version'] == expected_policy_version, 'stale_policy')
    need(type(expected_conversation_version) is int and snapshot['conversation_version'] == expected_conversation_version, 'stale_conversation')
    need(type(expected_context_digest) is str and re.fullmatch(r'[0-9a-f]{64}', expected_context_digest), 'context_digest_required')
    need(type(snapshot['messages']) is list and 1 <= len(snapshot['messages']) <= MAX_ADJACENT_MESSAGES + 1, 'snapshot_messages')
    raw = []
    for msg in snapshot['messages']:
        fields(msg, MESSAGE_FIELDS | {'message_hash'}, 'snapshot_message_fields')
        need(msg['message_hash'] == content_hash(msg['content']), 'source_hash_changed')
        raw.append({k: copy.deepcopy(msg[k]) for k in MESSAGE_FIELDS})
    need(raw[-1]['id'] == snapshot['current_message_id'], 'current_source_changed')
    rebuilt = capture_context(raw[-1], owner=snapshot['owner'], session=snapshot['session'],
        policy_version=snapshot['policy_version'], conversation_version=snapshot['conversation_version'],
        collect_types=snapshot['collect_types'], context=raw[:-1], source_after_id=snapshot['source_after_id'],
        excluded_ids=snapshot['excluded_ids'], context_complete=snapshot['context_complete'])
    need(rebuilt == snapshot and rebuilt['context_digest'] == expected_context_digest, 'context_changed')
    need(snapshot['context_complete'], 'context_incomplete')
    return rebuilt


def _ground(candidate, snapshot, origin):
    fields(candidate, {'contract', 'category', 'subject', 'content', 'time', 'relation', 'evidence'}, 'candidate_fields')
    need(len(wire(candidate)) <= MAX_RESPONSE_BYTES, 'candidate_budget')
    need(candidate['contract'] == CANDIDATE_CONTRACT, 'candidate_contract')
    category = candidate['category']
    need(type(category) is str and category in TYPES, 'category')
    need(category in snapshot['collect_types'], 'collection_disabled')
    need(candidate['subject'] == 'self', 'subject_requires_clarification_or_out_of_scope')
    need(type(candidate['relation']) is str and candidate['relation'] in RELATIONS, 'relation')
    evidence = candidate['evidence']
    need(type(evidence) is list and 1 <= len(evidence) <= MAX_ITEMS, 'evidence_count')
    sources = {'current': snapshot['messages'][-1], **{
        'context_' + str(i): msg for i, msg in enumerate(snapshot['messages'][:-1], 1)}}
    groundings = []; seen = set()
    for ref in evidence:
        fields(ref, {'source_ref', 'text'}, 'evidence_fields')
        need(type(ref['source_ref']) is str, 'evidence_source_ref')
        source = sources.get(ref['source_ref'])
        need(source is not None and source['role'] == 'user', 'evidence_not_owned_user_source')
        mid = source['id']
        quote = text(ref['text'], MAX_CURRENT_BYTES, 'evidence_text')
        start = source['content'].find(quote)
        need(start >= 0, 'quote_missing')
        need(source['content'].find(quote, start + 1) < 0, 'quote_ambiguous')
        need((mid, start, quote) not in seen, 'duplicate_evidence')
        seen.add((mid, start, quote))
        groundings.append({**copy.deepcopy(ref), 'message_id': mid, 'message_hash': source['message_hash'],
            'start': start, 'end': start + len(quote),
            'recorded_at': source['recorded_at']})
    need(any(r['message_id'] == snapshot['current_message_id'] for r in groundings), 'current_user_evidence_required')
    content = candidate['content']; time = candidate['time']
    fields(time, {'raw', 'temporal_scope'}, 'time_fields')
    if category == 'self_disease_report':
        fields(content, {'value_raw', 'polarity', 'certainty'}, 'disease_fields')
        value = text(content['value_raw'], 640, 'value_raw')
        need(any(value in r['text'] for r in groundings), 'value_not_in_user_evidence')
        need(type(content['polarity']) is str and content['polarity'] in POLARITIES, 'polarity')
        need(type(content['certainty']) is str and content['certainty'] in CERTAINTIES, 'certainty')
        need(type(time['temporal_scope']) is str and time['temporal_scope'] in {'current_report', 'past', 'unspecified'}, 'temporal_scope')
        if time['raw'] is not None:
            raw_time = text(time['raw'], 480, 'time_raw')
            need(any(raw_time in r['text'] for r in groundings), 'time_not_in_user_evidence')
        else:
            need(time['temporal_scope'] == 'unspecified', 'time_basis_required')
    else:
        fields(content, {'slot', 'value', 'scope'}, 'preference_fields')
        need(type(content['slot']) is str and content['slot'] in PREFERENCES and
             type(content['value']) is str and content['value'] in PREFERENCES[content['slot']], 'preference_value')
        need(content['scope'] == 'long_term', 'preference_not_long_term')
        need(time == {'raw': None, 'temporal_scope': 'not_applicable'}, 'preference_time')
    grounded = {'contract': GROUNDED_CONTRACT, 'candidate': copy.deepcopy(candidate), 'groundings': groundings,
        'origin': origin, 'context_digest': snapshot['context_digest'], 'policy_version': snapshot['policy_version'],
        'conversation_version': snapshot['conversation_version'],
        # Local packet, NOT a provider payload. R2 must remove authority fields.
        # Keep the entire bounded source context, not just selected quotes.
        'semantic_context': copy.deepcopy(snapshot['messages']), 'validation_level': 'structural_only',
        'requires_semantic_validation': True, 'writable': False, 'user_confirmed': False,
        'clinical_confirmation': False, 'writes_performed': False}
    return {**grounded, 'preview_id': digest([GROUNDED_CONTRACT, snapshot['context_digest'], candidate])[:32]}


def validate_candidate(candidate, snapshot, *, expected_policy_version, expected_conversation_version,
                       expected_context_digest, origin='model_proposal'):
    """Ground a non-writable proposal. Never grants semantic or storage approval."""
    need(type(origin) is str and origin in {'model_proposal', 'local_rule'}, 'origin')
    source = checked_context(snapshot, expected_policy_version=expected_policy_version,
        expected_conversation_version=expected_conversation_version, expected_context_digest=expected_context_digest)
    return _ground(candidate, source, origin)


def validate_response(raw, snapshot, *, expected_policy_version, expected_conversation_version, expected_context_digest):
    """Strict atomic parsing; an empty array is not a semantic negative verdict."""
    source = checked_context(snapshot, expected_policy_version=expected_policy_version,
        expected_conversation_version=expected_conversation_version, expected_context_digest=expected_context_digest)
    text(raw, MAX_RESPONSE_BYTES, 'response_budget')
    def pairs(items):
        obj = {}
        for key, value in items:
            need(key not in obj, 'duplicate_json_key'); obj[key] = value
        return obj
    def nonfinite(_):
        raise NaturalMemoryContractError('non_json_number')
    try:
        obj = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    except NaturalMemoryContractError:
        raise
    except (ValueError, RecursionError):
        raise NaturalMemoryContractError('invalid_response_json') from None
    fields(obj, {'contract', 'items'}, 'response_fields')
    need(obj['contract'] == RESPONSE_CONTRACT, 'response_contract')
    need(type(obj['items']) is list and len(obj['items']) <= MAX_ITEMS, 'candidate_count')
    results = []; seen = set()
    for item in obj['items']:
        result = _ground(item, source, 'model_proposal')
        identity = digest({k: item[k] for k in ['category', 'subject', 'content', 'time', 'relation']})
        need(identity not in seen, 'duplicate_candidate'); seen.add(identity); results.append(result)
    return {'contract': GROUNDED_CONTRACT, 'candidates': results, 'validation_level': 'structural_only',
        'requires_semantic_validation': True, 'writable': False, 'writes_performed': False,
        'context_digest': source['context_digest']}


def check_model_messages_budget(messages):
    """Validate the complete model messages at R2's future dispatch boundary."""
    need(type(messages) is list and 1 <= len(messages) <= 8, 'model_messages')
    for msg in messages:
        fields(msg, {'role', 'content'}, 'model_message_fields')
        need(type(msg['role']) is str and msg['role'] in {'system', 'user', 'assistant'}, 'model_role')
        text(msg['content'], MAX_REQUEST_BYTES, 'model_message_content')
    size = len(wire(messages)); need(size <= MAX_REQUEST_BYTES, 'complete_request_budget')
    return size
