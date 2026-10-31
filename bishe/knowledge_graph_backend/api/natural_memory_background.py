"""Durable background collection and independent review, with no request-thread SDK.

Chat writes a small outbox row only. A separate worker owns at most one extract
and one review attempt under the original deadline. Review survives later chat;
source, policy and target changes still invalidate it. Release gates default off.
"""
import copy,json,logging,uuid
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from .models import ChatMessage,Conversation
from .natural_memory_models import NaturalMemoryLedger,NaturalMemoryBackgroundTask,NaturalMemoryInbox
from .natural_memory_contract import NaturalMemoryContractError,need,fields,digest,text,validate_candidate
from .natural_memory_extraction import prepare_extraction,prepare_review,preview
from . import natural_memory_intake as intake,natural_memory_policy as decisions

log=logging.getLogger(__name__)
def available():
    return (getattr(settings,'NATURAL_MEMORY_BACKGROUND_ENABLED',False) is True and
        (getattr(settings,'NATURAL_MEMORY_ISOLATED_ENABLED',False) is True or
         getattr(settings,'NATURAL_MEMORY_FORMAL_ENABLED',False) is True and getattr(settings,'NATURAL_MEMORY_RELEASE_READY',False) is True))
def enabled(user):
    need(available(),'natural_memory_disabled');return intake.account(user)
def cancel_locked(user,reason):
    row=NaturalMemoryLedger.objects.filter(user=user).first()
    def invalid(source,permission_digest):
        if row is None:return True
        try:
            need(permission_digest==digest(row.permissions),'permission_changed');_source(row,source);return False
        except (NaturalMemoryContractError,KeyError,TypeError,ValueError):return True
    for task in NaturalMemoryBackgroundTask.objects.filter(user=user,status__in=['queued','running']):
        if invalid(task.snapshot,digest(task.permissions)):
            _clean_task(task,'stale',reason)
    for item in NaturalMemoryInbox.objects.filter(user=user,status='pending'):
        if invalid(item.payload.get('source'),item.payload.get('permission_digest')):
            item.status='stale';item.payload={};item.save(update_fields=['status','payload'])
def _source(row,source):
    live=intake._sources(row,source)
    decisions._source(source,row.state,row.policy,**{k:live[k] for k in ['live_sources','conversation_versions']})
    return live
def enqueue(user,message_id,*,context_ids=()):
    """Use an already persisted owned message, never duplicate a chat message."""
    enabled(user)
    with intake._locked(user) as row:
        old=NaturalMemoryBackgroundTask.objects.filter(user=user,source_message_id=message_id).first()
        if old:return {'status':'already_queued','task_key':old.task_key}
        if not row.policy['collect_types'] or not row.permissions['cloud_extraction']:
            return {'status':'collection_disabled'}
        need(type(context_ids) in [list,tuple] and len(context_ids)<=4,'context_budget')
        message=ChatMessage.objects.select_related('conversation').filter(pk=message_id,conversation__user=user,role='user').first()
        need(message is not None,'message_unavailable')
        need(NaturalMemoryBackgroundTask.objects.filter(user=user,status__in=['queued','running']).count()<32,'background_task_budget')
        source=intake._capture(row,message.conversation,message,list(context_ids))
        task=NaturalMemoryBackgroundTask.objects.create(user=user,source_message=message,task_key=uuid.uuid4().hex,
            snapshot=source,permissions=copy.deepcopy(row.permissions),deadline_at=message.created_at+timedelta(seconds=90))
        return {'status':'queued','task_key':task.task_key}
def enqueue_after_chat(user,message_id,*,context_ids=()):
    """An optional memory write may fail; the surrounding chat must still commit."""
    if not available() or not getattr(user,'is_authenticated',False):return {'status':'disabled'}
    try:
        with transaction.atomic():return enqueue(user,message_id,context_ids=context_ids)
    except Exception:
        # Never log user text, source snapshots, credentials or raw model output.
        log.warning('Optional natural-memory enqueue failed for message %s',message_id)
        return {'status':'not_queued'}
def _options(row,source):
    return {'permissions':row.permissions,'expected_policy_version':row.policy['version'],
        'expected_conversation_version':source['conversation_version'],'expected_context_digest':source['context_digest']}
def _plan(row,payload,*,allow_auto=False):
    source=payload['source'];live=_source(row,source);options=_options(row,source)
    need(payload['permission_digest']==digest(row.permissions),'permission_changed')
    choices=None;relation=payload.get('relation')
    if relation:
        result=preview(source,extraction_response=payload['extraction'],review_response=payload['review'],**options)
        candidates=[x['grounded']['candidate'] for x in result.get('supported',[])]
        if relation['operation']=='new_default' and result['status']=='pending_review':
            candidates=[x['candidate'] for x in result.get('unreviewed',[])]
        need(len(candidates)==1,'relation_single_candidate')
        need(relation['operation']!='new_default' or candidates[0]['category']=='presentation_preference','default_category')
        grounded=validate_candidate(candidates[0],source,**{k:v for k,v in options.items() if k!='permissions'})
        choices={grounded['preview_id']:{**relation,'event_state':None}}
    return decisions.propose(row.state,source,policy=row.policy,permissions=row.permissions,**live,
        expected_revision=row.revision,request_key='bg-propose-'+digest([payload['key'],row.revision,payload.get('relation')])[:32],extraction_response=payload['extraction'],
        review_response=payload['review'],choices=choices,allow_auto=allow_auto,allow_later_messages=True)
def _clean_task(task,status,reason=''):
    task.status=status;task.reason=reason;task.snapshot={};task.permissions={};task.extraction_reply='';task.save()
def _stage_attempt(user,key,stage):
    with intake._locked(user) as row:
        task=NaturalMemoryBackgroundTask.objects.select_for_update().get(user=user,task_key=key)
        need(task.status=='running','background_task_closed');need(timezone.now()<task.deadline_at,'background_deadline')
        _source(row,task.snapshot);need(task.permissions==row.permissions,'permission_changed')
        field=stage+'_calls';need(getattr(task,field)==0,'background_call_already_attempted')
        setattr(task,field,1);task.save(update_fields=[field])
        request=(prepare_extraction(task.snapshot,**_options(row,task.snapshot)) if stage=='extraction' else
            prepare_review(task.snapshot,task.extraction_reply,**_options(row,task.snapshot)))
        request['remaining_original_seconds']=(task.deadline_at-timezone.now()).total_seconds()
        request['constraints']['timeout_seconds_cap']=min(45,request['remaining_original_seconds'])
        return request
def run_task(user,key,*,provider):
    """Called only by the worker. Lost/running attempts never acquire a retry."""
    enabled(user)
    from .natural_memory_dispatch import provider_for_user,require_provider
    provider=provider_for_user(provider,user);require_provider(provider,user)
    with intake._locked(user):
        task=NaturalMemoryBackgroundTask.objects.select_for_update().get(user=user,task_key=key)
        if task.status!='queued':return {'status':task.status,'new_calls':0}
        task.status='running';task.save(update_fields=['status'])
    try:
        request=_stage_attempt(user,key,'extraction');raw=provider.extract(request)
        with intake._locked(user) as row:
            task=NaturalMemoryBackgroundTask.objects.select_for_update().get(user=user,task_key=key)
            need(task.status=='running' and timezone.now()<task.deadline_at,'background_deadline')
            _source(row,task.snapshot);need(task.permissions==row.permissions,'permission_changed')
            result=preview(task.snapshot,extraction_response=raw,**_options(row,task.snapshot))
            need(result['status'] not in ['processing_failed','blocked','needs_context'],'background_extraction_failed')
            task.extraction_reply=raw;task.audit={'extraction_hash':digest(raw)};task.save(update_fields=['extraction_reply','audit'])
        review=None
        if result['status']=='awaiting_review':
            request=_stage_attempt(user,key,'review');review=provider.review(request)
        with intake._locked(user) as row:
            task=NaturalMemoryBackgroundTask.objects.select_for_update().get(user=user,task_key=key)
            need(task.status=='running' and timezone.now()<task.deadline_at,'background_deadline')
            _source(row,task.snapshot);need(task.permissions==row.permissions,'permission_changed')
            payload={'key':key,'source':copy.deepcopy(task.snapshot),'permission_digest':digest(row.permissions),
                'extraction':task.extraction_reply,'review':review,'relation':None}
            live=_source(row,task.snapshot)
            result=_plan(row,payload,allow_auto=live['latest_user_ids'].get(task.snapshot['session'])==task.snapshot['current_message_id'])
            need(result['status'] not in ['processing_failed','blocked','needs_context'],'background_review_failed')
            state=result['state'];pending=state['pending'];state['pending']=None
            # Only effective changes advance the shared answer revision. A new
            # review card alone must not invalidate an unrelated live answer.
            if state['entries']!=row.state['entries']:
                intake._persist(row,decisions._seal(state));row.epoch+=1;row.save(update_fields=['epoch'])
                from .natural_memory_lifecycle import invalidate_locked
                invalidate_locked(user)
            task.audit.update(review_hash=digest(review) if review else '',result_status=result['status'])
            if pending:
                need(NaturalMemoryInbox.objects.filter(user=user,status='pending').count()<64,'inbox_budget')
                NaturalMemoryInbox.objects.create(user=user,task=task,item_key=uuid.uuid4().hex,payload=payload,
                    audit={'result_status':result['status'],'source_digest':task.snapshot['context_digest']})
            _clean_task(task,'completed')
            return {'status':result['status'],'saved_ack':result['status']=='projected_auto_preference'}
    except Exception as error:
        from .natural_memory_dispatch import fail_provider
        fail_provider(provider,'background_result_rejected')
        with intake._locked(user):
            task=NaturalMemoryBackgroundTask.objects.select_for_update().get(user=user,task_key=key)
            if task.status=='running':_clean_task(task,'failed',str(error) if isinstance(error,NaturalMemoryContractError) else 'background_provider_failed')
        return {'status':'processing_failed','saved_ack':False}

def _display(row,payload,plan):
    # A click on an old proposal is fresh explicit intent, after showing later
    # user statements. Bind the display to those statements, not just the old
    # model result. Never silently accept after another message arrives.
    source=payload['source'];cid=int(source['session'].split(':')[1]);mid=int(source['current_message_id'][3:])
    later=list(ChatMessage.objects.filter(conversation__user=row.user,conversation_id=cid,pk__gt=mid,role='user').order_by('pk')[:5])
    envelopes=[intake._envelope(m,row.user) for m in later]
    return {'display_digest':digest([plan['display_digest'],envelopes]),'later_messages':[
        {'content':m.content,'recorded_at':m.created_at.isoformat()} for m in later[:4]],
        'needs_fresh_statement':len(later)>4}

def read_state(user):
    enabled(user)
    from .natural_memory_answering import grants
    from .natural_memory_panel import FLAGS
    with transaction.atomic():
        row=NaturalMemoryLedger.objects.filter(user=user).first()
        if row is None:
            return {'status':'unconfigured','revision':0,'epoch':0,'policy_version':0,'answer_grant_version':0,
                'settings':{key:False for key in FLAGS},'items':[],'entries':[],'pending_count':0,'writes_performed':False}
        view=intake.read_state(user);items=[]
        pending=NaturalMemoryInbox.objects.filter(user=user,status='pending').order_by('pk')
        for item in pending[:32]:
            try:
                result=_plan(row,item.payload);plan=result['state']['pending'];need(plan is not None,'no_pending')
                actions=plan['actions'];source=item.payload['source']
                display=_display(row,item.payload,plan)
                items.append({'key':item.item_key,'status':'needs_context' if plan['unresolved'] or display['needs_fresh_statement'] else 'needs_confirmation',
                    **display,'conversation_id':int(source['session'].split(':')[1]),
                    'source_text':source['messages'][-1]['content'],'facts':[a['fact'] for a in actions]+plan['clarification_facts'],
                    'questions':[q['question']['question'] for q in plan['questions']],
                    'relation_required':any(a['operation']=='unresolved' for a in actions),
                    'matching_entries':[{'key':e['key'],'version':e['version'],'fact':e['fact']} for e in row.state['entries'].values()
                        if e['status'] in ['active','disputed'] and e['fact']]})
            except NaturalMemoryContractError:
                items.append({'key':item.item_key,'status':'unavailable','source_text':'','facts':[],'questions':[],
                    'relation_required':False,'matching_entries':[],'display_digest':None})
        grant=grants(user) or {'version':0,'cloud_routing':False,'cloud_answer_personal_context':False}
        return {'status':'configured','revision':row.revision,'epoch':row.epoch,'policy_version':row.policy['version'],
            'answer_grant_version':grant['version'],'entries':[{**entry,'source_conversation_id':
                int(row.state['entries'][entry['key']]['bindings'][0]['session'].split(':')[1])
                if row.state['entries'][entry['key']]['bindings'] else None} for entry in view['entries']],
            'items':items,'pending_count':pending.count(),
            'settings':{'collect_health':'self_disease_report' in row.policy['collect_types'],
                'collect_preferences':'presentation_preference' in row.policy['collect_types'],
                **{k:row.policy[k] for k in ['use_enabled','preference_auto_enabled']},
                **{k:row.permissions[k] for k in ['cloud_extraction','cloud_verification']},
                **{k:grant[k] for k in ['cloud_routing','cloud_answer_personal_context']}},
            'preference_auto_available':row.policy['preference_auto_gate_passed'],'writes_performed':False}

def update_settings(user,body):
    enabled(user)
    from .natural_memory_panel import FLAGS,VERSIONS
    from . import natural_memory_answering as answering
    fields(body,FLAGS|VERSIONS|{'request_key'},'settings_fields');need(all(type(body[k]) is bool for k in FLAGS),'settings_flags')
    intake._key(body['request_key']);fingerprint=digest(['background-settings',body])
    with transaction.atomic():
        if not NaturalMemoryLedger.objects.filter(user=user).exists():
            need(all(type(body[k]) is int and body[k]==0 for k in VERSIONS),'stale_state')
            intake.initialize_account(user)
            initial=True
        else:initial=False
        with intake._locked(user) as row:
            replay=intake._replay(user,body['request_key'],fingerprint)
            if replay is not None:return replay
            if not initial:intake._check(row,body)
            grant=answering.grants(user)
            need(body['expected_answer_grant_version']==(grant['version'] if grant else 0),'stale_answer_grant')
            types=[category for key,category in [('collect_health','self_disease_report'),('collect_preferences','presentation_preference')] if body[key]]
            intake.configure(user,expected_epoch=row.epoch,expected_policy_version=row.policy['version'],collect_types=types,
                use_enabled=body['use_enabled'],cloud_extraction=body['cloud_extraction'],cloud_verification=body['cloud_verification'],
                preference_auto_enabled=body['preference_auto_enabled'],preference_auto_gate_passed=row.policy['preference_auto_gate_passed'])
            row.refresh_from_db();answering.configure_grants(user,expected_version=body['expected_answer_grant_version'],expected_epoch=row.epoch,
                cloud_routing=body['cloud_routing'],cloud_answer_personal_context=body['cloud_answer_personal_context'])
            row.refresh_from_db();return intake._record(row,body['request_key'],fingerprint,'settings_updated')

def _item(row,key):
    need(type(key) is str and len(key)==32,'inbox_key')
    item=NaturalMemoryInbox.objects.select_for_update().filter(user=row.user,item_key=key,status='pending').first()
    need(item is not None,'inbox_unavailable');return item
def change_item(user,body):
    """Explicit management click; later unrelated chat does not revoke this intent."""
    enabled(user);fields(body,{'request_key','expected_revision','expected_epoch','expected_policy_version',
        'key','display_digest','decision'},'inbox_decision_fields')
    intake._key(body['request_key']);need(body['decision'] in ['accept','reject'],'confirmation_decision')
    fingerprint=digest(['background-decision',body])
    with intake._locked(user) as row:
        replay=intake._replay(user,body['request_key'],fingerprint)
        if replay is not None:return replay
        intake._check(row,body);item=_item(row,body['key'])
        if body['decision']=='reject' and body['display_digest'] is None:
            # An unavailable item may be discarded, but can never be accepted.
            item.status='rejected';item.payload={};item.save(update_fields=['status','payload'])
            return intake._record(row,body['request_key'],fingerprint,'rejected')
        staged=_plan(row,item.payload);plan=staged['state']['pending'];need(plan is not None,'no_pending')
        display=_display(row,item.payload,plan)
        need(body['display_digest']==display['display_digest'],'stale_inbox_display')
        need(body['decision']!='accept' or not display['needs_fresh_statement'],'fresh_statement_required')
        source=item.payload['source'];live=_source(row,source)
        event={'kind':'click','owner':intake.account(user),'session':source['session'],'display_digest':plan['display_digest'],
            'decision':body['decision'],'message_id':None}
        result=decisions.confirm(staged['state'],owner=intake.account(user),policy=row.policy,**live,
            expected_revision=staged['state']['revision'],request_key='bg-confirm-'+item.item_key,event=event,allow_later_messages=True)
        intake._persist(row,result['state']);row.epoch+=1;row.pending_epoch=None;row.save(update_fields=['epoch','pending_epoch'])
        item.status='accepted' if body['decision']=='accept' else 'rejected';item.payload={}
        item.audit['accepted_display_digest']=display['display_digest'];item.save(update_fields=['status','payload','audit'])
        from .natural_memory_lifecycle import invalidate_locked
        invalidate_locked(user)
        return intake._record(row,body['request_key'],fingerprint,result['status'],saved=body['decision']=='accept')

def revise_item(user,body):
    enabled(user);fields(body,{'request_key','expected_revision','expected_epoch','expected_policy_version',
        'key','display_digest','content','relation'},'inbox_revision_fields')
    intake._key(body['request_key']);fingerprint=digest(['background-revise',body])
    with intake._locked(user) as row:
        replay=intake._replay(user,body['request_key'],fingerprint)
        if replay is not None:return replay
        intake._check(row,body);item=_item(row,body['key']);staged=_plan(row,item.payload);plan=staged['state']['pending']
        need(plan is not None and _display(row,item.payload,plan)['display_digest']==body['display_digest'],'stale_inbox_display')
        if body['relation'] is not None:
            need(body['content'] is None,'inbox_revision_mode');relation=body['relation']
            fields(relation,{'operation','target','target_version'},'relation_fields')
            need(relation['operation'] in ['correction','new_event','new_default'],'relation_operation')
            candidate={**copy.deepcopy(item.payload),'relation':relation}
            _plan(row,candidate)  # Validate category, target, version and binding before storing a choice.
            item.payload=candidate;item.save(update_fields=['payload'])
            return intake._record(row,body['request_key'],fingerprint,'review_updated')
        text(body['content'],4096,'reply_budget');source=item.payload['source'];_source(row,source)
        need(len(source['messages'])<=4,'context_budget')
        conv=intake._conversation(user,int(source['session'].split(':')[1]))
        message=ChatMessage.objects.create(conversation=conv,role='user',content=body['content']);message.refresh_from_db()
        queued=enqueue(user,message.pk,context_ids=[int(m['id'][3:]) for m in source['messages']])
        need(queued['status']=='queued','reply_not_queued')
        item.status='replaced';item.payload={};item.audit['followup_task_key']=queued['task_key'];item.save(update_fields=['status','payload','audit'])
        return intake._record(row,body['request_key'],fingerprint,'reply_received',task_key=queued['task_key'])

def worker_once(*,provider=None,max_tasks=1):
    """Bounded management worker; never launched by a chat request or import."""
    need(available(),'natural_memory_disabled');need(type(max_tasks) is int and 1<=max_tasks<=32,'worker_budget')
    provider=provider or getattr(settings,'NATURAL_MEMORY_BACKGROUND_PROVIDER',None)
    from .natural_memory_dispatch import BoundedModelProvider
    need(getattr(provider,'offline_fixture',False) is True or isinstance(provider,BoundedModelProvider),'offline_provider_required')
    if isinstance(provider,BoundedModelProvider):provider._gate()
    results=[]
    for task in NaturalMemoryBackgroundTask.objects.filter(status='running',deadline_at__lte=timezone.now())[:32]:
        with intake._locked(task.user):
            task.refresh_from_db()
            if task.status=='running':_clean_task(task,'failed','interrupted_no_retry')
    for task in NaturalMemoryBackgroundTask.objects.filter(status='queued').select_related('user').order_by('pk')[:max_tasks]:
        results.append({'task_key':task.task_key,**run_task(task.user,task.task_key,provider=provider)})
    return results
