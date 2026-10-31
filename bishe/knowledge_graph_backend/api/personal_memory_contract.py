"""P01 pure policy over structured candidates, not language extraction or storage.

Reuses the existing source-span contract without changing legacy chat behavior.
Caller-supplied owner, grants, target revision and event association must come
from a trusted adapter. Matching source text does NOT prove its supplied meaning;
P03 must establish category, subject, assertion, intent and event association.
"""
import copy
from .memory_contract import bounded, require, timestamp, validate_record
from .user_memory_options import OPTIONS

VERSION = 'personal-policy-v1'
TYPE_RULES = {
    'self_disease_report': ('event', frozenset(), frozenset()),
    'self_allergy_report': ('event', frozenset({'reaction'}), frozenset()),
    'self_intolerance_report': ('event', frozenset({'reaction'}), frozenset()),
    'current_medication': ('event', frozenset({'dose_raw', 'frequency_raw'}), frozenset()),
    'temporary_health_state': ('event', frozenset(), frozenset()),
    'symptom_event': ('event', frozenset(), frozenset()),
    'measurement_event': ('event', frozenset({'value_raw', 'unit'}), frozenset({'value_raw', 'unit'})),
    'test_result_event': ('event', frozenset({'result_raw', 'unit'}), frozenset({'result_raw'})),
    'presentation_preference': ('preference', frozenset(), frozenset()),
    'service_relevant_background': ('background', frozenset(), frozenset()),
}
INTENTS = {'new_information', 'supplement', 'correction', 'same_event_update', 'new_default', 'forget', 'pause'}
UNASSERTED = {'question', 'hypothetical', 'quotation', 'assistant_inference'}


def validate_candidate(candidate, messages):
    """Validate shape and provenance; no inference and no clinical verification."""
    require(isinstance(candidate, dict) and set(candidate) == {'category', 'assertion', 'record', 'details'}, 'candidate_fields')
    category = candidate['category']
    require(isinstance(category, str) and category in TYPE_RULES, 'category')
    record = validate_record(candidate['record'], messages)
    kind, allowed, required = TYPE_RULES[category]
    require(record['kind'] == kind, 'category_kind')
    assertion = candidate['assertion']
    require(isinstance(assertion, str) and assertion in {'reported', 'suspected', 'denied', 'not_applicable'}, 'assertion')
    if kind == 'event':
        require(assertion != 'not_applicable', 'health_assertion')
        require((assertion == 'suspected' and record['event_state'] == 'uncertain') or
                (assertion == 'denied' and record['event_state'] == 'denied') or
                (assertion == 'reported' and record['event_state'] in {'current', 'past', 'resolved'}), 'assertion_state')
        require(record['storage_state'] == 'forgotten' or record['value'] in record['source']['text'], 'value_source')
        require(record['attribute'] == category + ':' + record['value'], 'category_attribute')
    else:
        require(assertion == 'not_applicable', 'non_health_assertion')
        key = record['attribute']
        allowed_keys = {'style', 'length'} if kind == 'preference' else {'background'}
        require(key in allowed_keys and record['value'] in OPTIONS[key]['values'], 'finite_presentation_value')
    details = candidate['details']
    require(isinstance(details, dict) and set(details) <= allowed and required <= set(details), 'details_fields')
    for value in details.values():
        require(bounded(value, 160) and value in record['source']['text'], 'detail_source')
    return copy.deepcopy({**candidate, 'record': record})


def decide_personal_update(candidate, messages, *, owner, allowed_types=(), old=None,
                           intent='new_information', target_id=None, expected_revision=None,
                           source_origin='current', history_sessions=(), excluded_sources=()):
    """Return a proposal; all authorizations and versions require commit recheck.

No newest-message-wins rule. Unknown event linkage is never guessed. Old-source
replays and denied grants cannot be overridden by a model's candidate fields.
"""
    require(bounded(owner), 'authenticated_owner')
    require(isinstance(intent, str) and intent in INTENTS, 'intent')
    require(isinstance(source_origin, str) and source_origin in {'current', 'history'}, 'source_origin')
    require(isinstance(allowed_types, (list, tuple, set, frozenset)) and
            all(isinstance(t, str) and t in TYPE_RULES for t in allowed_types), 'allowed_types')
    require(isinstance(history_sessions, (list, tuple, set, frozenset)) and
            all(bounded(s) for s in history_sessions), 'history_sessions')
    require(isinstance(excluded_sources, (list, tuple, set, frozenset)) and
            all(bounded(s) for s in excluded_sources), 'excluded_sources')
    new = validate_candidate(candidate, messages)
    prior = validate_candidate(old, messages) if old is not None else None
    r = new['record']; previous = prior['record'] if prior else None

    def result(action, reason):
        return {'contract': VERSION, 'action': action, 'reason': reason,
                'new_id': r['id'], 'old_id': previous['id'] if previous else None,
                'expected_old_revision': previous['revision'] if previous else None,
                'assertion': new['assertion'], 'writes_performed': False,
                'user_confirmed': False, 'clinical_confirmation': False}

    if r['owner'] != owner or previous and previous['owner'] != owner:
        return result('reject', 'different_owner')
    if r['source']['message_id'] in excluded_sources:
        return result('reject', 'excluded_source')
    if previous and previous['source']['message_id'] in excluded_sources:
        return result('reject', 'excluded_target_source')
    if source_origin == 'history' and r['session'] not in history_sessions:
        return result('reject', 'history_not_selected')
    if r['source']['mode'] in UNASSERTED:
        return result('reject', 'not_user_assertion')
    if r['subject'] == 'unknown':
        return result('clarify', 'subject_unknown')
    if r['subject'] != 'self':
        return result('reject', 'family_out_of_scope')
    if r['storage_state'] != 'active':
        return result('reject', 'candidate_not_active')
    if intent in {'forget', 'pause'}:
        if not previous or r['source']['mode'] != 'explicit_request':
            return result('clarify', 'explicit_lifecycle_target_required')
    elif new['category'] not in allowed_types:
        return result('reject', 'collection_not_enabled')
    if intent == 'new_default' and r['kind'] == 'event':
        return result('reject', 'health_event_not_presentation_default')
    if intent in {'correction', 'new_default'} and r['source']['mode'] != 'explicit_request':
        return result('clarify', 'explicit_correction_or_default_required')
    if intent in {'correction', 'same_event_update', 'supplement', 'forget', 'pause'}:
        if (not previous or target_id != previous['id'] or type(expected_revision) is not int or
                expected_revision != previous['revision']):
            return result('clarify', 'exact_current_target_required')
    if intent == 'new_default' and previous and (target_id != previous['id'] or
            type(expected_revision) is not int or expected_revision != previous['revision']):
        return result('clarify', 'exact_current_target_required')
    if previous and previous['id'] == r['id']:
        return result('no_change', 'identical_replay') if prior == new and intent == 'new_information' else result('reject', 'immutable_id')
    if previous and previous['storage_state'] != 'active':
        return result('reject', 'forgotten_source_replay') if previous['source']['message_id'] == r['source']['message_id'] else result('clarify', 'inactive_target')
    if intent in {'forget', 'pause'}:
        if (previous['subject'], prior['category'], previous['attribute']) != (r['subject'], new['category'], r['attribute']):
            return result('clarify', 'lifecycle_target_mismatch')
        return result(intent, 'explicit_lifecycle_request')
    if r['scope'] != 'long_term':
        return result('temporary_only', 'narrow_scope_not_long_term')
    if previous is None:
        initial_default = intent == 'new_default' and r['kind'] in {'preference', 'background'}
        return result('add', 'sourced_self_report_or_preference') if intent == 'new_information' or initial_default else result('clarify', 'missing_target')
    if previous['subject'] != r['subject']:
        return result('clarify', 'subject_correction_requires_distinct_retraction')
    if (prior['category'], previous['attribute']) != (new['category'], r['attribute']):
        return result('keep_separate', 'different_information') if intent == 'new_information' else result('clarify', 'different_update_attribute')
    if previous['scope'] != 'long_term':
        return result('clarify', 'non_long_term_target')
    if r['kind'] == 'event' and previous['event_id'] and r['event_id'] and previous['event_id'] != r['event_id']:
        return result('keep_separate', 'distinct_events') if intent == 'new_information' else result('clarify', 'different_event_target')
    # Timestamp is checked only for attempts that might modify the same fact.
    # Distinct historical observations may be added without displacing it.
    if timestamp(r['recorded_at']) < timestamp(previous['recorded_at']):
        return result('clarify', 'older_source_not_current_update')
    if r['kind'] == 'event':
        same_event = bool(r['event_id'] and r['event_id'] == previous['event_id'])
        if not same_event:
            return result('clarify', 'event_identity_required')
        if intent == 'same_event_update':
            return result('event_transition', 'sourced_same_event_change')
        if intent == 'correction':
            return result('replace_effective', 'explicit_targeted_correction_keep_history')
        same_state = (r['event_state'], new['assertion'], r['event_time']) == (previous['event_state'], prior['assertion'], previous['event_time'])
        if not same_state:
            return result('clarify', 'state_conflict_requires_relationship')
    elif intent == 'same_event_update':
        return result('reject', 'not_event')
    elif intent in {'correction', 'new_default'}:
        if r['source']['mode'] != 'explicit_request':
            return result('clarify', 'explicit_default_or_correction_required')
        return result('replace_effective', 'explicit_default_or_correction_keep_history')
    if prior['details'] == new['details'] and previous['value'] == r['value']:
        return result('no_change', 'same_effective_information')
    changed_fields = {k for k in prior['details'] if k not in new['details'] or prior['details'][k] != new['details'][k]}
    if not changed_fields and intent == 'supplement':
        return result('supplement', 'additional_sourced_details_preserve_existing')
    return result('clarify', 'value_or_details_conflict')
