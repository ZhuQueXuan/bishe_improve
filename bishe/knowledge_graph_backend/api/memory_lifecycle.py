"""Finite lifecycle: tombstone sources and bounded version/lease checked upkeep.

No clinical state inference, time-based health deletion, LLM or vector access.
Plans contain only owned IDs/versions/reasons; commit recomputes the plan.
"""
import hashlib
import json
import re
import uuid
from datetime import timedelta
from django.db import transaction,DatabaseError
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .models import (Conversation,ChatMessage,MemorySettings,MemoryInputEvent,HealthMemory,
                     HealthMemoryProposal,MemorySourceExclusion,MemoryMaintenanceTask)

def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
def excluded_ids(user):return set(MemorySourceExclusion.objects.filter(user=user).values_list('message_id',flat=True))
def exclude_sources(user,ids):
    # A caller cannot tombstone messages belonging to another account.
    allowed=ChatMessage.objects.filter(pk__in=set(ids),role='user',conversation__user=user).values_list('id',flat=True)
    for mid in allowed:MemorySourceExclusion.objects.get_or_create(user=user,message_id=mid)

def exclude_health_sources(row):
    ids=list(HealthMemoryProposal.objects.filter(user=row.user,status='committed',result_id=row.id).exclude(source_message_id=None).values_list('source_message_id',flat=True))
    mid=row.report.get('source',{}).get('message_id','')
    if mid.startswith('db:') and mid[3:].isdigit():ids.append(int(mid[3:]))
    exclude_sources(row.user,ids)

def exclude_preference_source(row):
    match=re.fullmatch(r'conversation:(\d+)/(.+)',row.source_message_ref or '')
    if not match:return
    cid=int(match[1]);key=hashlib.sha256(f'{cid}:{match[2]}'.encode()).hexdigest()
    event=MemoryInputEvent.objects.filter(user=row.user,event_id=key).first()
    if event and event.source_message_id:exclude_sources(row.user,[event.source_message_id]);return
    # Legacy entries have no message FK. Exclude only literal source-bearing
    # user messages in that owned conversation, never global similar texts.
    if row.source_text:
        mids=list(ChatMessage.objects.filter(conversation_id=cid,conversation__user=row.user,role='user',
                                            content__contains=row.source_text).order_by('id').values_list('id',flat=True)[:2001])
        if len(mids)>2000:raise ValidationError('原来源范围过大，请先明确整理相关会话。')
        exclude_sources(row.user,mids)

class HistoryRows(list):
    pass

def filtered_history(conversation,rows):
    blocked=excluded_ids(conversation.user);out=[];skip=False
    for row in rows:
        if row['role']=='user':skip=int(row['id'][3:]) in blocked
        if not skip:out.append(row)
    selected=HistoryRows(out)
    selected.source_cursor=max([conversation.memory_after_id]+[int(r['id'][3:]) for r in rows])
    return selected

def link_input_source(user,conversation,mid,message):
    if not mid:return
    key=hashlib.sha256(f'{conversation.id}:{mid}'.encode()).hexdigest()
    MemoryInputEvent.objects.filter(user=user,event_id=key,source_message__isnull=True).update(source_message=message)

def serialize_task(task):
    return {'id':task.id,'status':task.status,'attempts':task.attempts,'profile_version':task.profile_version,
            'result':task.result,'updated_at':task.updated_at.isoformat()}

def create_task(user,request_key,version):
    if not isinstance(request_key,str) or not 1<=len(request_key)<=64 or type(version) is not int:
        raise ValidationError('整理请求标识或版本无效。')
    with transaction.atomic():
        s,_=MemorySettings.objects.get_or_create(user=user)
        s=MemorySettings.objects.select_for_update().get(pk=s.pk)
        prior=MemoryMaintenanceTask.objects.filter(user=user,request_key=request_key).first()
        if prior:
            if prior.profile_version!=version:raise ValidationError('同一请求标识不能用于不同版本。')
            return prior
        if s.version!=version:raise ValidationError('记忆已更新，请刷新后再整理。')
        pending=MemoryMaintenanceTask.objects.filter(user=user,status__in=['queued','running']).first()
        if pending:return pending
        return MemoryMaintenanceTask.objects.create(user=user,request_key=request_key,profile_version=version)

def claim_task(user,tid):
    with transaction.atomic():
        task=MemoryMaintenanceTask.objects.select_for_update().filter(pk=tid,user=user).first()
        if not task:raise ValidationError('整理任务不存在。')
        if task.status in {'completed','stale','exhausted'}:return task,None
        if task.status=='running' and task.lease_until and task.lease_until>timezone.now():return task,None
        if task.attempts>=2:
            task.status='exhausted';task.result={'reason':'attempt_limit'};task.save();return task,None
        task.status='running';task.attempts+=1;task.lease_token=uuid.uuid4().hex
        task.lease_until=timezone.now()+timedelta(seconds=60);task.save()
        return task,task.lease_token

def build_plan(user):
    from .controlled_health import stored_report,proposal_report
    from .memory_contract import MemoryContractError
    from .summary_layers import VERSION
    blocked=excluded_ids(user);actions=[];inputs=[]
    health=list(HealthMemory.objects.filter(user=user).exclude(status='forgotten').order_by('id')[:51])
    proposals=list(HealthMemoryProposal.objects.filter(user=user,status='pending').select_related('source_message__conversation').order_by('id')[:51])
    conversations=list(Conversation.objects.filter(user=user).exclude(context_notes={}).order_by('id')[:51])
    if any(len(rows)>50 for rows in [health,proposals,conversations]):
        # No silent partial maintenance; no unbounded account-wide scans.
        return {'fingerprint':'over_bound','actions':[],'requires_narrowing':True}
    s=MemorySettings.objects.filter(user=user).first();version=s.version if s else 0
    inputs.append(['profile',version,s.health_collect_enabled if s else False,sorted(blocked)])
    identical={}
    for row in health:
        try:r=stored_report(row)
        except (ValueError,TypeError,KeyError,MemoryContractError):r=None
        inputs.append(['health',row.id,row.version,row.status,fingerprint(row.report),bool(r)])
        if not r:
            if row.status!='invalid':actions.append({'kind':'invalid_health','id':row.id,'version':row.version,'reason':'source_unverifiable'})
            continue
        # Reconcile receipt source exclusions without reviving data or copying it.
        mid=r['source']['message_id']
        if mid.startswith('db:') and int(mid[3:]) in blocked:
            actions.append({'kind':'invalid_health','id':row.id,'version':row.version,'reason':'source_excluded'});continue
        canonical={k:v for k,v in r.items() if k not in {'id','revision'}}
        signature=fingerprint(canonical)
        if row.status=='active' and signature in identical:
            actions.append({'kind':'archive_duplicate','id':row.id,'version':row.version,'canonical_id':identical[signature]})
        elif row.status=='active':identical[signature]=row.id
    for p in proposals:
        valid=True
        try:proposal_report(p)
        except (ValidationError,ValueError,KeyError,MemoryContractError):valid=False
        expired=timezone.now()-p.created_at>timedelta(minutes=10)
        inputs.append(['proposal',p.id,p.status,p.profile_version,p.conversation_version,p.input_hash,valid,expired])
        if expired or not valid or p.profile_version!=version or not s or not s.health_collect_enabled:
            actions.append({'kind':'expire_proposal','id':p.id})
    for c in conversations:
        cache=c.context_notes;stale=not isinstance(cache,dict)
        if not stale:
            partition=f'conversation:{c.id}'
            if c.memory_version or version:partition+=f':m{c.memory_version}:p{version}'
            stale=(cache.get('revision')!=f'{c.memory_version}:{version}' or cache.get('version')!=VERSION
                   or cache.get('partition')!=partition)
            recent=cache.get('recent',[]);summary=cache.get('summary',{})
            notes=summary.get('notes',[]) if isinstance(summary,dict) else []
            if not isinstance(summary,dict) or not isinstance(recent,list) or not isinstance(notes,list):stale=True;recent=[];notes=[]
            for r in recent+notes:
                if not isinstance(r,dict):stale=True;continue
                mid=r.get('source_message_id','');src=r.get('source',{})
                if not mid.startswith('db:') or not mid[3:].isdigit():stale=True;continue
                message=ChatMessage.objects.filter(pk=int(mid[3:]),conversation=c).first()
                stale=stale or int(mid[3:]) in blocked or message is None or not isinstance(src,dict)
                if message and isinstance(src,dict):stale=stale or src.get('sha256')!=hashlib.sha256(message.content.encode()).hexdigest()
            # Optional derivation can age out; underlying health records cannot.
            stale=stale or timezone.now()-c.updated_at>timedelta(days=7)
        inputs.append(['cache',c.id,c.memory_version,fingerprint(cache),bool(stale)])
        if stale:actions.append({'kind':'clear_cache','id':c.id,'cache_hash':fingerprint(cache)})
    return {'fingerprint':fingerprint(inputs),'actions':actions,'requires_narrowing':False}

def commit_plan(user,tid,token,plan):
    with transaction.atomic():
        s=MemorySettings.objects.select_for_update().get(user=user)
        task=MemoryMaintenanceTask.objects.select_for_update().get(pk=tid,user=user)
        if task.status!='running' or task.lease_token!=token:return task
        if task.lease_until is None or task.lease_until<=timezone.now():return task
        current=build_plan(user)
        if s.version!=task.profile_version or current!=plan:
            task.status='stale';task.result={'reason':'version_or_sources_changed'};task.lease_token='';task.save();return task
        if plan['requires_narrowing']:
            task.status='completed';task.result={'requires_narrowing':True,'changed':0};task.lease_token='';task.save();return task
        changed=0;profile_changed=False;counts={}
        for a in plan['actions']:
            kind=a['kind'];counts[kind]=counts.get(kind,0)+1
            if kind in {'invalid_health','archive_duplicate'}:
                row=HealthMemory.objects.get(pk=a['id'],user=user,version=a['version'])
                row.status='invalid' if kind=='invalid_health' else 'archived';row.version+=1;row.save()
                profile_changed=True;changed+=1
            elif kind=='expire_proposal':
                changed+=HealthMemoryProposal.objects.filter(pk=a['id'],user=user,status='pending').update(status='expired')
            elif kind=='clear_cache':changed+=Conversation.objects.filter(pk=a['id'],user=user).update(context_notes={})
        if profile_changed:
            s.version+=1;s.save(update_fields=['version']);Conversation.objects.filter(user=user).update(context_notes={})
        task.status='completed';task.result={'changed':changed,'counts':counts,'profile_version':s.version,'requires_narrowing':False}
        task.lease_token='';task.lease_until=None;task.save();return task

def run_task(user,tid):
    task,token=claim_task(user,tid)
    if token is None:return task
    try:return commit_plan(user,tid,token,build_plan(user))
    except (DatabaseError,ValueError,TypeError,KeyError):
        # Atomic commit has rolled back; never claim maintenance success.
        MemoryMaintenanceTask.objects.filter(pk=tid,user=user,status='running',lease_token=token).update(
            status='failed',result={'reason':'maintenance_failed_no_success_claim'},lease_token='',lease_until=None)
        return MemoryMaintenanceTask.objects.get(pk=tid,user=user)
