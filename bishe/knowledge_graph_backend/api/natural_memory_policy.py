"""R3 isolated, copy-on-write decisions. No database, transport or chat caller.

State, policy, live envelopes, latest IDs and typed user intents must be supplied
by a trusted authenticated adapter. Digests detect drift; they are not signatures
or proof of user intent. R4 must persist the returned state with an atomic CAS.
"""
import copy

from .natural_memory_contract import (MESSAGE_FIELDS, TYPES, checked_context,
    digest, fields, need, stamp, text, wire)
from .natural_memory_extraction import preview

STATE = 'natural-memory-decision-state-v1'
POLICY = 'natural-memory-decision-policy-v1'
OPERATIONS = {'add', 'duplicate', 'supplement', 'same_event_update', 'new_event',
    'correction', 'new_default', 'retraction', 'unresolved'}
MANAGEMENT = {'forget', 'pause', 'resume', 'retraction', 'keep_original', 'undo'}


def _seal(state):
    state['seal'] = digest({k: v for k, v in state.items() if k != 'seal'})
    return state


def new_state(owner):
    text(owner, 80, 'owner')
    need(owner.startswith('user:') and owner[5:].isdigit(), 'owner')
    return _seal({'contract': STATE, 'owner': owner, 'revision': 0, 'entries': {},
        'pending': None, 'receipts': {}, 'blocked_sources': []})


def _checked(state, policy, owner, expected_revision):
    fields(state, {'contract', 'owner', 'revision', 'entries', 'pending', 'receipts',
        'blocked_sources', 'seal'}, 'state_fields')
    need(state['contract'] == STATE and state['owner'] == owner, 'state_owner')
    need(type(state['revision']) is int and state['revision'] >= 0, 'state_revision')
    need(state['seal'] == digest({k: v for k, v in state.items() if k != 'seal'}), 'state_integrity')
    need(type(state['entries']) is dict and len(state['entries']) <= 50, 'entry_budget')
    need(type(state['receipts']) is dict and len(state['receipts']) <= 128, 'receipt_budget')
    need(type(state['blocked_sources']) is list and len(state['blocked_sources']) <= 512, 'blocked_budget')
    need(len(wire(state)) <= 1048576, 'state_budget')
    fields(policy, {'contract', 'owner', 'version', 'collect_types', 'use_enabled',
        'preference_auto_enabled', 'preference_auto_gate_passed'}, 'policy_fields')
    need(policy['contract'] == POLICY and policy['owner'] == owner, 'policy_owner')
    need(type(policy['version']) is int and policy['version'] > 0, 'policy_version')
    need(type(policy['collect_types']) is list and all(type(x) is str and x in TYPES
        for x in policy['collect_types']) and len(set(policy['collect_types'])) == len(policy['collect_types']), 'policy_types')
    need(all(type(policy[x]) is bool for x in ['use_enabled', 'preference_auto_enabled',
        'preference_auto_gate_passed']), 'policy_boolean')
    need(type(expected_revision) is int and expected_revision >= 0, 'expected_revision')
    return copy.deepcopy(state)


def _begin(state, request_key, fingerprint, expected_revision):
    text(request_key, 100, 'request_key')
    previous = state['receipts'].get(request_key)
    if previous is not None:
        need(previous['fingerprint'] == fingerprint, 'request_key_reused')
        return {'state': state, 'status': 'replayed', 'state_changed': False,
            'receipt_revision': previous['revision'], 'writes_performed': False,
            'saved_ack': False, 'requires_persistence_recheck': True}
    need(state['revision'] == expected_revision, 'stale_state')
    need(len(state['receipts']) < 128, 'receipt_budget')
    return None


def _finish(state, key, fingerprint, status, **extra):
    state['revision'] += 1
    state['receipts'][key] = {'fingerprint': fingerprint, 'revision': state['revision']}
    _seal(state)
    need(len(wire(state)) <= 1048576, 'state_budget')
    return {'state': state, 'status': status, 'state_changed': True,
        'writes_performed': False, 'saved_ack': False, 'requires_persistence_recheck': True,
        'clinical_confirmation': False, **extra}


def _envelope(message):
    return {k: message[k] for k in MESSAGE_FIELDS}


def _binding(source):
    return {'owner': source['owner'], 'session': source['session'],
        'conversation_version': source['conversation_version'],
        'messages': [{'id': m['id'], 'fingerprint': digest(_envelope(m))} for m in source['messages']]}


def _live(binding, state, live_sources, conversation_versions):
    need(binding['owner'] == state['owner'], 'source_owner')
    need(conversation_versions.get(binding['session']) == binding['conversation_version'], 'stale_conversation')
    for bound in binding['messages']:
        need(bound['id'] not in state['blocked_sources'], 'source_forgotten')
        message = live_sources.get(bound['id'])
        need(type(message) is dict and set(message) == MESSAGE_FIELDS, 'source_missing')
        need(message['owner'] == state['owner'] and message['session'] == binding['session'], 'source_owner')
        need(digest(message) == bound['fingerprint'], 'source_changed')


def _source(source, state, policy, live_sources, conversation_versions):
    need(source['owner'] == state['owner'], 'source_owner')
    need(sorted(policy['collect_types']) == source['collect_types'], 'collection_changed')
    checked_context(source, expected_policy_version=policy['version'],
        expected_conversation_version=conversation_versions.get(source['session']),
        expected_context_digest=source['context_digest'])
    _live(_binding(source), state, live_sources, conversation_versions)


def _fact(grounded):
    candidate = grounded['candidate']
    return {k: copy.deepcopy(candidate[k]) for k in ['category', 'subject', 'content', 'time']}


def _attribute(fact):
    category = fact['category']
    return (category, fact['content']['value_raw'] if category == 'self_disease_report' else fact['content']['slot'])


def _history(entry):
    need(len(entry['history']) < 20, 'history_budget')
    entry['history'].append({k: copy.deepcopy(v) for k, v in entry.items() if k != 'history'})
    entry['version'] += 1


def _dispute(entry):
    if entry['status'] == 'active':
        _history(entry)
        entry['status'] = 'disputed'


def _entry_live(entry, state, live_sources, conversation_versions):
    for binding in entry['bindings']:
        _live(binding, state, live_sources, conversation_versions)
    proof = entry['confirmation']
    if proof is not None:
        message = live_sources.get(proof['source_id'])
        need(proof['source_id'] not in state['blocked_sources'] and type(message) is dict
            and digest(message) == proof['source_fingerprint'], 'confirmation_source_changed')


def entry_display_digest(entry):
    """Display binding; an authenticated adapter must still establish ownership."""
    return digest(entry)


def _union(first, second):
    result = copy.deepcopy(first)
    for item in second:
        if item not in result:
            result.append(copy.deepcopy(item))
    return result


def _apply(state, actions, confirmed, confirmation=None):
    for index, action in enumerate(actions):
        operation = action['operation']; target = action['target']; fact = action['fact']
        if target is None:
            need(len(state['entries']) < 50, 'entry_budget')
            key = digest([state['owner'], state['revision'], action, index])[:32]
            need(key not in state['entries'], 'entry_key_collision')
            state['entries'][key] = {'key': key, 'version': 1, 'status': 'active', 'fact': fact,
                'event_id': key if fact['category'] == 'self_disease_report' else None,
                'event_state': action['event_state'] or 'reported', 'sources': action['sources'],
                'bindings': [action['binding']], 'user_confirmed': confirmed,
                'semantic_assurance': action['semantic_assurance'],
                'clinical_confirmation': False, 'confirmation': copy.deepcopy(confirmation), 'history': []}
            continue
        entry = state['entries'][target]
        need(entry['version'] == action['target_version'], 'stale_target')
        need(entry_display_digest(entry) == action['target_digest'], 'target_changed')
        need(entry['status'] not in {'forgotten', 'retracted', 'paused'}, 'target_inactive')
        _history(entry)
        if operation == 'retraction':
            entry['status'] = 'retracted'
        else:
            entry['fact'] = fact
            entry['status'] = 'active'
            entry['event_state'] = action['event_state'] or entry['event_state']
        entry['sources'] = _union(entry['sources'], action['sources'])
        entry['bindings'] = _union(entry['bindings'], [action['binding']])
        entry['semantic_assurance'] = action['semantic_assurance']
        # A repeated mention never removes a prior user confirmation.
        entry['user_confirmed'] = entry['user_confirmed'] or confirmed
        if confirmed:
            entry['confirmation'] = copy.deepcopy(confirmation)


def propose(state, source, *, policy, permissions, live_sources, conversation_versions,
            latest_user_ids, expected_revision, request_key, extraction_response,
            review_response=None, force_review=False, choices=None, allow_auto=True, allow_later_messages=False):
    """Rebuild R2 from raw replies; stage a display, never trust a client preview.

    choices are explicit server-resolved user choices, not model relation fields.
    A different disease name cannot implicitly select a correction target.
    """
    working = _checked(state, policy, source['owner'], expected_revision)
    choices = {} if choices is None else choices
    need(type(choices) is dict, 'choices_fields')
    need(type(allow_auto) is bool, 'auto_type')
    need(type(allow_later_messages) is bool, 'background_intent_type')
    identity = ['propose', source, policy, permissions, extraction_response,
        review_response, force_review, choices, allow_auto, expected_revision]
    fingerprint = digest(identity + (['background'] if allow_later_messages else []))
    replay = _begin(working, request_key, fingerprint, expected_revision)
    if replay is not None:
        return replay
    _source(source, working, policy, live_sources, conversation_versions)
    need(allow_later_messages or latest_user_ids.get(source['session']) == source['current_message_id'], 'newer_user_message')
    result = preview(source, permissions=permissions, expected_policy_version=policy['version'],
        expected_conversation_version=conversation_versions[source['session']],
        expected_context_digest=source['context_digest'], extraction_response=extraction_response,
        review_response=review_response, force_review=force_review)
    # A new intake supersedes the old display, including empty/failed intake.
    working['pending'] = None
    candidates = [x['grounded'] for x in result.get('supported', [])]
    if result['status'] == 'pending_review':
        candidates = result['unreviewed']  # only R2's narrowly bounded pure preferences
    need(set(choices) <= {x['preview_id'] for x in candidates}, 'choice_candidate')
    for rejected in result.get('contradicted', []):
        if rejected['review']['reason'] == 'wrong_subject':
            attr = _attribute(_fact(rejected['grounded']))
            for entry in working['entries'].values():
                if entry['fact'] and _attribute(entry['fact']) == attr:
                    _dispute(entry)
    if result['status'] not in {'proposed', 'pending_review', 'needs_confirmation'}:
        return _finish(working, request_key, fingerprint, result['status'])
    actions = []; unresolved = bool(result.get('questions') or result.get('unclear'))
    used_targets = set(); attributes = {}
    for grounded in candidates:
        fact = _fact(grounded); attr = _attribute(fact)
        matches = [e for e in working['entries'].values() if e['fact'] and _attribute(e['fact']) == attr
            and e['status'] in {'active', 'disputed'}]
        choice = choices.get(grounded['preview_id'])
        if choice is None:
            operation = 'unresolved' if matches or grounded['candidate']['relation'] not in {'new_information', 'new_event'} else 'add'
            target = None; event_state = None
            if fact['category'] == 'presentation_preference' and len(matches) == 1:
                target = matches[0]['key']
                operation = 'duplicate' if matches[0]['fact'] == fact else 'new_default'
        else:
            fields(choice, {'operation', 'target', 'target_version', 'event_state'}, 'choice_fields')
            operation, target, event_state = choice['operation'], choice['target'], choice['event_state']
            need(type(operation) is str and operation in OPERATIONS, 'choice_operation')
            need(event_state is None or type(event_state) is str and event_state in {'reported', 'resolved'}, 'event_state')
            need(event_state is None or operation == 'same_event_update', 'event_state_operation')
            if target is None:
                need(choice['target_version'] is None and operation in {'add', 'new_event', 'unresolved'}, 'choice_target')
                need(operation != 'add' or not matches, 'relationship_required')
            else:
                need(type(target) is str and target in working['entries'], 'choice_target')
                entry = working['entries'][target]
                need(entry['version'] == choice['target_version'], 'stale_target')
                need(entry['status'] in {'active', 'disputed'}, 'target_inactive')
                need(operation in {'duplicate', 'supplement', 'same_event_update', 'correction', 'new_default', 'retraction'}, 'choice_target')
        if target is not None:
            need(target not in used_targets, 'duplicate_target')
            used_targets.add(target); entry = working['entries'][target]
            _entry_live(entry, working, live_sources, conversation_versions)
            need(entry['fact']['category'] == fact['category'], 'target_category')
            if operation != 'correction':
                need(_attribute(entry['fact']) == attr, 'target_attribute')
            if operation == 'duplicate':
                need(entry['fact'] == fact and entry['status'] == 'active', 'not_duplicate')
            if operation == 'supplement':
                need(entry['fact']['content'] == fact['content'], 'supplement_changes_assertion')
                need(entry['fact']['time']['raw'] is None or fact['time'] == entry['fact']['time'], 'supplement_replaces_time')
            if operation == 'new_default':
                need(fact['category'] == 'presentation_preference', 'default_category')
            if operation == 'same_event_update':
                need(fact['category'] == 'self_disease_report', 'event_category')
            # Do not silently discard an earlier explicit event time.
            if operation in {'correction', 'same_event_update'} and fact['time']['raw'] is None:
                fact['time'] = copy.deepcopy(entry['fact']['time'])
            if operation == 'correction':
                _dispute(entry)
        if operation == 'new_event':
            need(fact['category'] == 'self_disease_report', 'event_category')
        # A reviewed withdrawal of self attribution must stop the matching
        # report being used while its target/relationship is still unresolved.
        # Keep provenance and require the existing explicit resolution flow.
        if fact['category'] == 'self_disease_report' and grounded['candidate']['relation'] == 'retraction':
            for old in matches:
                _dispute(old)
        if attr in attributes and not (operation == 'new_event' and attributes[attr] == 'new_event'):
            unresolved = True  # contradictory/ambiguous candidates cannot both become current by insertion order
        attributes[attr] = operation
        if fact['category'] == 'self_disease_report' and operation != 'new_event':
            for old in matches:
                same_time = not old['fact']['time']['raw'] or not fact['time']['raw'] or old['fact']['time'] == fact['time']
                if same_time and old['fact']['content'] != fact['content']:
                    _dispute(old)
        unresolved |= operation == 'unresolved'
        actions.append({'operation': operation, 'target': target,
            'target_version': working['entries'][target]['version'] if target else None,
            'target_digest': entry_display_digest(working['entries'][target]) if target else None,
            'previous_fact': copy.deepcopy(working['entries'][target]['fact']) if target else None,
            'semantic_assurance': result.get('semantic_assurance'),
            'fact': fact, 'event_state': event_state, 'sources': copy.deepcopy(grounded['groundings']),
            'binding': _binding(source)})
    auto = allow_auto and bool(actions) and not unresolved and policy['preference_auto_enabled'] and policy['preference_auto_gate_passed']
    auto = auto and all(a['fact']['category'] == 'presentation_preference' and a['operation'] == 'add' for a in actions)
    if auto:
        _apply(working, actions, False)
        return _finish(working, request_key, fingerprint, 'projected_auto_preference')
    plan = {'source': copy.deepcopy(source), 'policy_hash': digest(policy),
        'state_revision': working['revision'] + 1, 'actions': actions, 'unresolved': unresolved,
        'semantic_assurance': result.get('semantic_assurance'),
        'clarification_facts': [_fact(x['grounded']) for x in result.get('unclear', [])],
        'questions': copy.deepcopy(result.get('questions', []))}
    plan['display_digest'] = digest(plan)
    working['pending'] = plan
    return _finish(working, request_key, fingerprint, 'needs_clarification' if unresolved else 'needs_confirmation',
        display_digest=plan['display_digest'])


def _intent(event, *, owner, session, display_digest, decision, live_sources, latest_user_ids,
            original=None, allow_later_messages=False):
    fields(event, {'kind', 'owner', 'session', 'display_digest', 'decision', 'message_id'}, 'intent_fields')
    need(event['owner'] == owner and event['session'] == session, 'intent_owner')
    need(event['display_digest'] == display_digest and event['decision'] == decision, 'intent_binding')
    latest = latest_user_ids.get(session)
    message = live_sources.get(latest)
    need(type(message) is dict and set(message) == MESSAGE_FIELDS and message['owner'] == owner
        and message['session'] == session and message['role'] == 'user', 'intent_latest_source')
    need(event['kind'] in {'click', 'reply'}, 'intent_kind')
    if event['kind'] == 'click':
        need(event['message_id'] is None, 'intent_click_message')
        need(allow_later_messages or original is None or latest == original['current_message_id'], 'newer_user_message')
    else:
        need(event['message_id'] == latest, 'intent_reply_message')
        if original is not None:
            need(int(latest.split(':')[1]) > int(original['current_message_id'].split(':')[1]), 'intent_reply_order')
            need(stamp(message['recorded_at']) >= stamp(original['messages'][-1]['recorded_at']), 'intent_reply_time')
    return {'kind': event['kind'], 'source_id': latest, 'source_fingerprint': digest(message),
        'display_digest': display_digest, 'decision': decision}


def confirm(state, *, owner, policy, live_sources, conversation_versions, latest_user_ids,
            expected_revision, request_key, event, allow_later_messages=False):
    working = _checked(state, policy, owner, expected_revision)
    need(type(allow_later_messages) is bool, 'background_intent_type')
    fingerprint = digest(['confirm', policy, event, expected_revision] + (['background'] if allow_later_messages else []))
    replay = _begin(working, request_key, fingerprint, expected_revision)
    if replay is not None:
        return replay
    plan = working['pending']
    need(type(plan) is dict, 'no_pending')
    need(plan['display_digest'] == digest({k: v for k, v in plan.items() if k != 'display_digest'}), 'plan_integrity')
    need(plan['state_revision'] == working['revision'], 'stale_plan')
    need(plan['policy_hash'] == digest(policy), 'policy_changed')
    _source(plan['source'], working, policy, live_sources, conversation_versions)
    need(type(event) is dict, 'intent_fields')
    decision = event.get('decision')
    need(decision in {'accept', 'reject'}, 'confirmation_decision')
    intent = _intent(event, owner=owner, session=plan['source']['session'], display_digest=plan['display_digest'],
        decision=decision, live_sources=live_sources, latest_user_ids=latest_user_ids, original=plan['source'], allow_later_messages=allow_later_messages)
    if decision == 'accept':
        need(not plan['unresolved'], 'clarification_required')
        for action in plan['actions']:
            if action['target']:
                entry = working['entries'][action['target']]
                need(entry['version'] == action['target_version'], 'stale_target')
                need(entry_display_digest(entry) == action['target_digest'], 'target_changed')
                _entry_live(entry, working, live_sources, conversation_versions)
        _apply(working, plan['actions'], True, intent)
    working['pending'] = None
    return _finish(working, request_key, fingerprint, 'projected_confirmed' if decision == 'accept' else 'rejected')


def resolve(state, source, *, owner, policy, permissions, live_sources, conversation_versions,
            latest_user_ids, expected_revision, request_key, event, extraction_response,
            review_response=None, force_review=False, choices=None):
    """One targeted, typed reply can resolve a displayed question and confirm.

    The trusted adapter must establish the reply's explicit intent. Both old and
    new evidence are rechecked; a newly invented/changed assertion needs a fresh
    display instead. This function does not interpret words like 'yes'.
    """
    working = _checked(state, policy, owner, expected_revision)
    fingerprint = digest(['resolve', source, policy, permissions, event, extraction_response,
        review_response, force_review, choices, expected_revision])
    replay = _begin(working, request_key, fingerprint, expected_revision)
    if replay is not None:
        return replay
    old = working['pending']
    need(type(old) is dict and old['unresolved'], 'no_clarification')
    need(old['state_revision'] == working['revision'] and old['policy_hash'] == digest(policy), 'stale_plan')
    need(old['display_digest'] == digest({k: v for k, v in old.items() if k != 'display_digest'}), 'plan_integrity')
    _source(old['source'], working, policy, live_sources, conversation_versions)
    intent = _intent(event, owner=owner, session=old['source']['session'], display_digest=old['display_digest'],
        decision='resolve', live_sources=live_sources, latest_user_ids=latest_user_ids, original=old['source'])
    need(event['kind'] == 'reply' and source['current_message_id'] == event['message_id'], 'resolution_reply')
    need({m['id'] for m in old['source']['messages']} <= {m['id'] for m in source['messages']}, 'resolution_context_missing')
    need(len(working['receipts']) < 127, 'receipt_budget')
    staged = propose(working, source, policy=policy, permissions=permissions, live_sources=live_sources,
        conversation_versions=conversation_versions, latest_user_ids=latest_user_ids,
        expected_revision=expected_revision, request_key='resolve-stage:' + digest(request_key)[:32],
        extraction_response=extraction_response, review_response=review_response,
        force_review=force_review, choices=choices, allow_auto=False)
    if staged['status'] == 'no_memory':
        return _finish(staged['state'], request_key, fingerprint, 'projected_resolved_without_change')
    need(staged['status'] == 'needs_confirmation', 'resolution_not_ready')
    working = staged['state']; plan = working['pending']
    allowed = [a['fact'] for a in old['actions']] + old['clarification_facts']
    need(bool(plan['actions']), 'resolution_no_fact')
    if not allowed and old['questions']:
        # A subject/reference question may have had no candidate at all. The
        # reply's first grounded assertion needs a fresh display, not an
        # implicit confirmation. Rebind the plan to this final receipt's
        # revision so the displayed confirmation remains usable and replay-safe.
        plan['state_revision'] = working['revision'] + 1
        plan['display_digest'] = digest({k: v for k, v in plan.items() if k != 'display_digest'})
        return _finish(working, request_key, fingerprint, 'needs_confirmation',
            display_digest=plan['display_digest'])
    for action in plan['actions']:
        fact = action['fact']
        need(any(all(fact[k] == previous[k] for k in ['category', 'subject', 'content'])
            and (previous['time']['raw'] is None or fact['time'] == previous['time']) for previous in allowed),
            'resolution_changes_assertion')
        if action['target']:
            entry = working['entries'][action['target']]
            need(entry['version'] == action['target_version'], 'stale_target')
            need(entry_display_digest(entry) == action['target_digest'], 'target_changed')
            _entry_live(entry, working, live_sources, conversation_versions)
    intent['resolved_display_digest'] = plan['display_digest']
    _apply(working, plan['actions'], True, intent)
    working['pending'] = None
    return _finish(working, request_key, fingerprint, 'projected_confirmed_by_resolution')


def preference_before_latest_change(entry, state, *, live_sources, conversation_versions):
    """A live prior preference value; never a way to restore forgotten health."""
    need(entry['status']=='active' and entry['fact'] is not None and entry['fact']['category']=='presentation_preference','undo_preference_only')
    need(bool(entry['history']),'undo_history_required')
    previous=entry['history'][-1]
    need(previous['status']=='active' and previous['fact'] is not None and previous['fact']['category']=='presentation_preference', 'undo_history_required')
    need(previous['fact']['content']['slot']==entry['fact']['content']['slot'] and previous['fact']!=entry['fact'],'undo_value_not_changed')
    _entry_live(previous,state,live_sources,conversation_versions)
    return copy.deepcopy(previous)


def manage(state, *, owner, policy, live_sources, conversation_versions, latest_user_ids,
           expected_revision, request_key, target, target_version, action, event):
    """Typed authenticated UI/reply intent, not a natural-language command parser."""
    working = _checked(state, policy, owner, expected_revision)
    fingerprint = digest(['manage', policy, target, target_version, action, event, expected_revision])
    replay = _begin(working, request_key, fingerprint, expected_revision)
    if replay is not None:
        return replay
    need(type(action) is str and action in MANAGEMENT, 'management_action')
    need(type(target) is str and target in working['entries'], 'management_target')
    entry = working['entries'][target]
    need(entry['version'] == target_version, 'stale_target')
    need(entry['status'] != 'forgotten', 'already_forgotten')
    need(type(event) is dict, 'intent_fields')
    _intent(event, owner=owner, session=event.get('session'), display_digest=entry_display_digest(entry),
        decision=action, live_sources=live_sources, latest_user_ids=latest_user_ids)
    if action in {'keep_original', 'resume'}:
        need(entry['status'] == ('disputed' if action == 'keep_original' else 'paused'), 'management_status')
        need(bool(entry['bindings']) and bool(entry['sources']), 'fresh_evidence_required')
        _entry_live(entry, working, live_sources, conversation_versions)
    working['pending'] = None  # invalidate every previously displayed plan
    if action == 'undo':
        previous=preference_before_latest_change(entry,working,live_sources=live_sources,conversation_versions=conversation_versions)
        _history(entry)  # The restored content has a NEW monotonic version.
        for field in ['status','fact','event_id','event_state','sources','bindings','user_confirmed','clinical_confirmation','semantic_assurance','confirmation']:
            entry[field]=copy.deepcopy(previous[field])
    elif action == 'forget':
        ids = {m['id'] for version in [entry, *entry['history']] for b in version['bindings'] for m in b['messages']}
        ids |= {version['confirmation']['source_id'] for version in [entry, *entry['history']] if version['confirmation']}
        need(len(set(working['blocked_sources']) | ids) <= 512, 'blocked_budget')
        working['blocked_sources'] = sorted(set(working['blocked_sources']) | ids)
        # Clear all versions' extracted content; the chat envelopes themselves are untouched.
        working['entries'][target] = {'key': target, 'version': entry['version'] + 1, 'status': 'forgotten',
            'fact': None, 'event_id': None, 'event_state': None, 'sources': [], 'bindings': [],
            'user_confirmed': False, 'clinical_confirmation': False, 'semantic_assurance': None,
            'confirmation': None, 'history': []}
        # Other facts can quote the same sentence and retain forgotten text.
        # Scrub linked extracted quote copies from every version, keeping the
        # unrelated canonical assertion paused until fresh evidence is supplied.
        for other_key, other in working['entries'].items():
            if other_key == target or other['status'] == 'forgotten':
                continue
            affected = any(m['id'] in ids for version in [other, *other['history']]
                for binding in version['bindings'] for m in binding['messages'])
            affected |= any(version['confirmation'] and version['confirmation']['source_id'] in ids
                for version in [other, *other['history']])
            if not affected:
                continue
            # Do not allocate another private snapshot or let a full history
            # budget prevent erasure. Existing versions are scrubbed in place.
            other['version'] += 1
            for version in [other, *other['history']]:
                version['sources'] = [quote for quote in version['sources'] if quote['message_id'] not in ids]
                version['bindings'] = [binding for binding in version['bindings']
                    if not any(m['id'] in ids for m in binding['messages'])]
                if version['confirmation'] and version['confirmation']['source_id'] in ids:
                    version['confirmation'] = None
                    version['user_confirmed'] = False
            other['status'] = 'paused'
    else:
        _history(entry)
        entry['status'] = {'pause': 'paused', 'retraction': 'retracted', 'resume': 'active', 'keep_original': 'active'}[action]
    return _finish(working, request_key, fingerprint, 'projected_' + action)


def eligible_entries(state, *, owner, policy, live_sources, conversation_versions):
    """Qualification only; R5 query relevance and answer selection remain pending."""
    working = _checked(state, policy, owner, state['revision'])
    if not policy['use_enabled']:
        return []
    from .natural_memory_contract import NaturalMemoryContractError
    result = []
    for entry in working['entries'].values():
        if entry['status'] != 'active' or entry['event_state'] == 'resolved':
            continue
        if not entry['bindings'] or not entry['sources']:
            continue
        try:
            _entry_live(entry, working, live_sources, conversation_versions)
        except NaturalMemoryContractError:
            continue
        result.append(copy.deepcopy(entry))
    return result
