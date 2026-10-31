"""R5 answer orchestration with offline or explicitly authorized bounded providers.

Actual model dispatch, deployment and generalized checkpoint invalidation remain
separate gates. Durable tickets retain audit IDs/hashes, never prompt copies.
"""
import copy
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import ChatMessage
from .natural_memory_models import NaturalMemoryLedger, NaturalMemoryOperation, NaturalMemoryAnswerGrant
from .natural_memory_contract import NaturalMemoryContractError, digest, need
from . import natural_memory_intake as intake
from . import natural_memory_retrieval as retrieval


def grants(user):
    row = NaturalMemoryAnswerGrant.objects.filter(user=user).first()
    return None if row is None else {'version': row.version, 'cloud_routing': row.cloud_routing,
        'cloud_answer_personal_context': row.cloud_answer_personal_context}


def configure_grants(user, *, expected_version, expected_epoch, cloud_routing, cloud_answer_personal_context):
    """Trusted fixture administration, never client-provided or inherited."""
    with intake._locked(user) as ledger:
        need(type(expected_epoch) is int and ledger.epoch == expected_epoch, 'stale_epoch')
        need(type(expected_version) is int and expected_version >= 0, 'answer_grant_version')
        need(type(cloud_routing) is bool and type(cloud_answer_personal_context) is bool, 'answer_grant_flags')
        row = NaturalMemoryAnswerGrant.objects.filter(user=user).first()
        need((row.version if row else 0) == expected_version, 'stale_answer_grant')
        if row is None:
            row = NaturalMemoryAnswerGrant(user=user, version=0)
        row.version += 1; row.cloud_routing = cloud_routing; row.cloud_answer_personal_context = cloud_answer_personal_context
        row.save()
        intake._persist(ledger, intake._clear(ledger.state)); ledger.epoch += 1; ledger.pending_epoch = None
        ledger.save(update_fields=['epoch', 'pending_epoch']); intake._invalidate_jobs(user)
        return grants(user)


def _current(ledger, ticket, source):
    need(ledger.revision == ticket['revision'] and ledger.epoch == ticket['epoch'], 'stale_answer_context')
    need(digest(ledger.policy) == ticket['policy_hash'], 'stale_answer_policy')
    current_grants = grants(ledger.user)
    need(current_grants == ticket['grants'], 'stale_answer_grant')
    need(timezone.now() < timezone.datetime.fromisoformat(ticket['deadline_at']), 'answer_deadline')
    need(digest(source) == ticket['source_fingerprint'], 'question_snapshot_changed')
    live = intake._sources(ledger, source)
    from .natural_memory_policy import _source
    _source(source, ledger.state, ledger.policy, **{k: live[k] for k in ['live_sources', 'conversation_versions']})
    need(live['latest_user_ids'].get(source['session']) == source['current_message_id'], 'newer_user_message')
    return live, current_grants


def _step(user, key, phase, source):
    with intake._locked(user) as ledger:
        operation = NaturalMemoryOperation.objects.get(user=user, request_key=key)
        ticket = operation.receipt
        _current(ledger, ticket, source)
        need(ticket['status'] == 'answer_processing', 'answer_ticket_closed')
        field = phase + '_calls'
        need(ticket[field] == 0, 'answer_call_already_attempted')
        ticket[field] = 1; ticket['phase'] = phase
        operation.receipt = ticket; operation.save(update_fields=['receipt'])
        return ticket


def _terminal(user, key, status, *, reason=None):
    with intake._locked(user) as ledger:
        operation = NaturalMemoryOperation.objects.get(user=user, request_key=key)
        ticket = operation.receipt
        ticket.update(status='stale' if ticket.get('status') == 'stale' else status, reason=reason, source={})
        operation.receipt = ticket; operation.save(update_fields=['receipt'])
        return {'status': status, 'reason': reason, 'answer': None, 'saved_ack': False,
            'message_id': ticket['message_id'], 'writes_performed': True}


def _limited(request, ticket):
    result = copy.deepcopy(request)
    remaining = (timezone.datetime.fromisoformat(ticket['deadline_at']) - timezone.now()).total_seconds()
    need(remaining > 0, 'answer_deadline')
    result['remaining_original_seconds'] = remaining
    result['constraints']['timeout_seconds_cap'] = min(45, remaining)
    return result


def run_answer(user, message_id, *, context_ids, providers, pause_before_answer=False, omit_unpermitted_context=False):
    """Internal orchestration reached from a persisted, authenticated message.

    Providers are trusted server adapters, never HTTP callables or client model
    replies. Bounded real dispatch requires separate release and budget gates.
    """
    owner = intake.account(user)
    from .natural_memory_dispatch import require_provider
    require_provider(providers,user)
    need(type(omit_unpermitted_context) is bool, 'answer_context_omission_type')
    key = 'r5-answer-' + str(message_id)
    with intake._locked(user) as ledger:
        old = NaturalMemoryOperation.objects.filter(user=user, request_key=key).first()
        if old:
            return {'status': 'answer_already_processed', 'message_id': message_id,
                'writes_performed': False, 'saved_ack': False, 'answer': None}
        message = ChatMessage.objects.select_related('conversation').filter(pk=message_id, conversation__user=user, role='user').first()
        need(message is not None, 'question_unavailable')
        conv = intake._conversation(user, message.conversation_id)
        source = intake._capture(ledger, conv, message, context_ids)
        permission = grants(user)
        need(permission is not None, 'answer_not_configured')
        ticket = {'status': 'answer_processing', 'phase': 'received', 'revision': ledger.revision,
            'epoch': ledger.epoch, 'message_id': message.pk, 'policy_hash': digest(ledger.policy), 'grants': permission,
            'source_fingerprint': digest(source), 'deadline_at': (message.created_at + timedelta(seconds=90)).isoformat(),
            'routing_calls': 0, 'answer_calls': 0, 'selected': [], 'excluded': [], 'writes_performed': True,
            'saved_ack': False, 'omit_unpermitted_context': omit_unpermitted_context}
        intake._operation_budget(user)
        NaturalMemoryOperation.objects.create(user=user, request_key=key, input_hash=digest([owner, message_id, source]), receipt=ticket)
    try:
        request = retrieval.routing_request(source, permission)
        _step(user, key, 'routing', source)
        intent_raw = providers.route(_limited(request, ticket))
        with intake._locked(user) as ledger:
            live, permission = _current(ledger, ticket, source)
        intent = retrieval.validate_intent(intent_raw, source, permission)
        # Professional retrieval receives the question and purpose, never the
        # saved personal reports. Existing RAG retrieval is not changed here.
        evidence = providers.professional(source['messages'][-1]['content'], intent['purpose'])
        with intake._locked(user) as ledger:
            live, permission = _current(ledger, ticket, source)
            request = retrieval.answer_request(ledger.state, ledger.policy, source, permission, intent_raw, evidence,
                omit_unpermitted_context=ticket.get('omit_unpermitted_context', False),
                **{k: live[k] for k in ['live_sources', 'conversation_versions']})
            operation = NaturalMemoryOperation.objects.get(user=user, request_key=key)
            audit = operation.receipt
            selection = request['selection']
            audit['selected'] = [{'key': x['entry']['key'], 'version': x['entry']['version'], 'reason': x['reason']}
                for x in selection['selected']]
            audit['excluded'] = selection['excluded']
            audit['presentation_preferences'] = [{k: item[k] for k in ['slot', 'origin', 'key', 'version'] if k in item}
                for item in selection['preferences']]
            audit['purpose'] = intent['purpose']; audit['intent_hash'] = digest(intent_raw)
            audit['evidence_hash'] = digest(evidence)
            if request['status'] == 'ready':
                audit['model_input_hash'] = digest(request['messages'])
            operation.receipt = audit; operation.save(update_fields=['receipt'])
        if request['status'] != 'ready':
            return _terminal(user, key, request['status'])
        if pause_before_answer:
            from .natural_memory_lifecycle import checkpoint_locked, save_locked
            with intake._locked(user) as ledger:
                _current(ledger, ticket, source)
                operation = NaturalMemoryOperation.objects.get(user=user, request_key=key)
                audit = operation.receipt
                minimal = {k: copy.deepcopy(request[k]) for k in
                    ['status', 'request_ref', 'messages', 'input_bytes', 'payload', 'constraints']}
                checkpoint = checkpoint_locked(ledger, source=source, ticket_key=key, ticket=audit,
                    intent_raw=intent_raw, evidence=evidence, model_request=minimal)
                deadline = timezone.datetime.fromisoformat(audit['deadline_at'])
                cache = save_locked(ledger, 'cache', {'messages': request['messages'],
                    'input_hash': digest(request['messages'])}, source, expires=deadline)
                summary = save_locked(ledger, 'summary', {'personal_reports': request['payload']['personal_reports'],
                    'presentation_preferences': request['payload']['presentation_preferences'], 'clinical_confirmation': False}, source)
                audit.update(status='answer_paused', phase='before_answer', checkpoint_key=checkpoint,
                    cache_key=cache, summary_key=summary)
                operation.receipt = audit; operation.save(update_fields=['receipt'])
                return {'status': 'answer_paused', 'checkpoint_key': checkpoint, 'answer': None,
                    'revision': ledger.revision, 'epoch': ledger.epoch, 'deadline_at': audit['deadline_at'],
                    'routing_calls': audit['routing_calls'], 'answer_calls': audit['answer_calls'], 'saved_ack': False}
        return _complete(user, key, ticket, source, intent_raw, evidence, request, providers)
    except NaturalMemoryContractError as error:
        from .natural_memory_dispatch import fail_provider
        fail_provider(providers,'answer_result_rejected')
        return _terminal(user, key, 'answer_unavailable', reason=str(error))
    except Exception:
        from .natural_memory_dispatch import fail_provider
        fail_provider(providers,'answer_provider_or_storage_failure')
        # Never leak provider exceptions, credentials, SQL, or private payloads.
        return _terminal(user, key, 'answer_failed', reason='provider_or_storage_failure')


def _complete(user, key, ticket, source, intent_raw, evidence, request, providers):
    from .natural_memory_dispatch import require_provider
    require_provider(providers,user)
    _step(user, key, 'answer', source)
    with intake._locked(user) as ledger:
        live, permission = _current(ledger, ticket, source)
        fresh = retrieval.answer_request(ledger.state, ledger.policy, source, permission, intent_raw, evidence,
            omit_unpermitted_context=ticket.get('omit_unpermitted_context', False),
            **{k: live[k] for k in ['live_sources', 'conversation_versions']})
        need(fresh['status'] == 'ready' and digest(fresh['messages']) == digest(request['messages']), 'stale_answer_input')
    model_job = {k: copy.deepcopy(request[k]) for k in ['request_ref', 'messages', 'input_bytes', 'payload', 'constraints']}
    raw = providers.answer(_limited(model_job, ticket))
    answer = retrieval.validate_answer(raw, request)
    from .natural_memory_lifecycle import save_locked, delivery_locked
    with intake._locked(user) as ledger:
        live, permission = _current(ledger, ticket, source)
        fresh = retrieval.answer_request(ledger.state, ledger.policy, source, permission, intent_raw, evidence,
            omit_unpermitted_context=ticket.get('omit_unpermitted_context', False),
            **{k: live[k] for k in ['live_sources', 'conversation_versions']})
        need(fresh['status'] == 'ready' and digest(fresh['messages']) == digest(request['messages']), 'stale_answer_input')
        conv = intake._conversation(user, int(source['session'].split(':')[1]))
        response = ChatMessage.objects.create(conversation=conv, role='assistant', content=answer['answer'],
            has_context=bool(request['payload']['personal_reports'] or evidence))
        saved_preferences = any(x['origin'] == 'saved' for x in request['payload']['presentation_preferences'])
        result = {'status': 'answered', 'message_id': int(source['current_message_id'][3:]), 'answer_message_id': response.pk,
            'answer': answer['answer'], 'personal_context_used': bool(answer['personal_refs'] or saved_preferences),
            'health_context_used': bool(answer['personal_refs']), 'saved_preferences_used': saved_preferences,
            'clinical_confirmation': False, 'semantic_assurance': answer['semantic_assurance'],
            'basis': {'personal_reports': [copy.deepcopy(r) for r in request['payload']['personal_reports']
                    if r['ref'] in answer['personal_refs']],
                'presentation_preferences': copy.deepcopy(request['payload']['presentation_preferences']),
                'professional_evidence': [copy.deepcopy(r) for r in request['payload']['professional_evidence']
                    if r['ref'] in answer['professional_refs']]},
            'writes_performed': True, 'saved_ack': False}
        deadline = timezone.datetime.fromisoformat(ticket['deadline_at'])
        cache = save_locked(ledger, 'cache', {'messages': request['messages'], 'input_hash': digest(request['messages'])}, source, expires=deadline)
        summary = save_locked(ledger, 'summary', {'personal_reports': request['payload']['personal_reports'],
            'presentation_preferences': request['payload']['presentation_preferences'], 'clinical_confirmation': False}, source)
        delivery = save_locked(ledger, 'answer_view', result, source)
        operation = NaturalMemoryOperation.objects.get(user=user, request_key=key)
        audit = operation.receipt
        audit.update(status='answered', source={}, answer_hash=digest(raw), answer_message_id=response.pk,
            personal_refs=answer['personal_refs'], professional_refs=answer['professional_refs'],
            cache_key=cache, summary_key=summary, delivery_key=delivery)
        operation.receipt = audit; operation.save(update_fields=['receipt'])
        return delivery_locked(ledger, delivery)


def continue_answer(user, payload, *, providers):
    key = payload['ticket_key']
    try:
        return _complete(user, key, payload['ticket'], payload['source'], payload['intent_raw'],
            payload['evidence'], payload['request'], providers)
    except NaturalMemoryContractError as error:
        from .natural_memory_dispatch import fail_provider
        fail_provider(providers,'continued_answer_rejected')
        return _terminal(user, key, 'answer_unavailable', reason=str(error))
    except Exception:
        from .natural_memory_dispatch import fail_provider
        fail_provider(providers,'continued_answer_provider_failure')
        return _terminal(user, key, 'answer_failed', reason='provider_or_storage_failure')


def post_question(user, body, *, pause_before_answer=False):
    accepted = intake.post_message(user, body)
    if accepted['status'] == 'replayed':
        return accepted
    providers = getattr(settings, 'NATURAL_MEMORY_FIXTURE_PROVIDERS', None)
    if providers is None:
        return {**accepted, 'answer_status': 'not_configured'}
    try:
        answered = run_answer(user, accepted['message_id'], context_ids=body['context_ids'], providers=providers,
            pause_before_answer=pause_before_answer)
    except NaturalMemoryContractError as error:
        answered = {'status': 'answer_unavailable', 'reason': str(error), 'answer': None}
    return {**accepted, 'answer_status': answered['status'], 'answer_result': answered}
