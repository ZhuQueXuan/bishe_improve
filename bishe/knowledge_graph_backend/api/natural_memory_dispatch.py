"""Release-gated, explicitly authorized, durable bounded model provider.

No credentials/network are touched at import, approval, construction or lookup.
Only the transport child may read credentials. DB audit stores hashes/usage;
full fictional acceptance replies are captured by an explicit external sink.
"""
import copy,math,re,time
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.db import transaction,IntegrityError,OperationalError
from django.db.models import F
from django.utils import timezone
from .natural_memory_contract import need,digest,check_model_messages_budget,NaturalMemoryContractError
from .natural_memory_models import NaturalMemoryDispatchBudget as Budget,NaturalMemoryDispatchAttempt as Attempt
from .natural_memory_dispatch_transport import MODEL,request_once

STAGES={'extraction','review','query_intent','answer'}
RESERVE=(Decimal(15000)*Decimal('.30')+Decimal(1024)*Decimal('1.20'))/1000000

def approve_budget(*,batch_key,authorization_hash,owner_ids,stages,maximum_calls,budget_cny,expires_at):
    """Trusted operator only. A digest is a record of approval, not approval itself."""
    from .natural_memory_intake import isolated
    isolated();need(getattr(settings,'NATURAL_MEMORY_DISPATCH_CANDIDATE_ENABLED',False) is True,'dispatch_disabled')
    need(isinstance(batch_key,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',batch_key),'dispatch_batch_key')
    need(isinstance(authorization_hash,str) and re.fullmatch(r'[a-f0-9]{64}',authorization_hash),'dispatch_authorization')
    need(type(owner_ids) is list and owner_ids and all(type(x) is int and x>0 for x in owner_ids),'dispatch_owners')
    need(type(stages) is list and stages and set(stages)<=STAGES,'dispatch_stages')
    need(type(maximum_calls) is int and 1<=maximum_calls<=64,'dispatch_call_limit')
    amount=Decimal(str(budget_cny))/8;need(amount.is_finite() and amount>0,'dispatch_budget')
    need(timezone.is_aware(expires_at) and expires_at>timezone.now(),'dispatch_expiry')
    return Budget.objects.create(batch_key=batch_key,authorization_hash=authorization_hash,approved=True,
        owner_ids=sorted(set(owner_ids)),stages=sorted(set(stages)),maximum_calls=maximum_calls,budget_usd=amount,expires_at=expires_at)

def close_budget(key,reason):
    Budget.objects.filter(batch_key=key).update(closed=True,reason=reason[:80])

def provider_for_user(provider,user):
    if getattr(provider,'offline_fixture',False) is True:return provider
    if isinstance(provider,BoundedModelProvider):return provider.for_user(user)
    return None

def require_provider(provider,user):
    if getattr(provider,'offline_fixture',False) is True:return
    need(isinstance(provider,BoundedModelProvider),'offline_provider_required')
    provider.check_owner(user.pk)

def fail_provider(provider,reason):
    """A downstream format/source/permission failure also closes a paid batch."""
    if isinstance(provider,BoundedModelProvider):close_budget(provider.batch_key,reason)

class BoundedModelProvider:
    offline_fixture=False
    def __init__(self,batch_key,*,counter,professional,audit_sink,verify_frozen,owner_id=None,transport=None):
        self.batch_key=batch_key;self.counter=counter;self.retrieve=professional;self.sink=audit_sink;self.verify=verify_frozen
        self.owner_id=owner_id;self.transport=transport
    def _gate(self):
        from .natural_memory_intake import isolated
        isolated();need(getattr(settings,'NATURAL_MEMORY_DISPATCH_CANDIDATE_ENABLED',False) is True,'dispatch_disabled')
        if self.transport is None:need(getattr(settings,'NATURAL_MEMORY_LIVE_DISPATCH_ENABLED',False) is True,'live_dispatch_disabled')
        else:need(getattr(settings,'NATURAL_MEMORY_ISOLATED_ENABLED',False) is True,'test_transport_isolated_only')
    def check_owner(self,owner):
        self._gate();need(type(owner) is int and owner==self.owner_id,'dispatch_owner')
        budget=Budget.objects.get(batch_key=self.batch_key)
        need(budget.approved and not budget.closed and timezone.now()<budget.expires_at,'dispatch_closed_or_expired')
        need(owner in budget.owner_ids,'dispatch_owner')
    def for_user(self,user):
        bound=copy.copy(self);bound.owner_id=user.pk;bound.check_owner(user.pk);return bound
    def professional(self,question,purpose):return self.retrieve(question,purpose)
    def extract(self,request):return self.call(request,'extraction')
    def review(self,request):return self.call(request,'review')
    def route(self,request):return self.call(request,'query_intent')
    def answer(self,request):return self.call(request,'answer')
    def _reserve(self,request,stage,timeout):
        with transaction.atomic():
            need(Budget.objects.filter(batch_key=self.batch_key).update(lock_version=F('lock_version')+1)==1,'dispatch_budget_missing')
            budget=Budget.objects.get(batch_key=self.batch_key)
            need(budget.approved and not budget.closed and timezone.now()<budget.expires_at,'dispatch_closed_or_expired')
            need(self.owner_id in budget.owner_ids and stage in budget.stages,'dispatch_scope')
            need(not Attempt.objects.filter(budget=budget,status='dispatching',deadline_at__lte=timezone.now()).exists(),'dispatch_interrupted_unknown')
            need(budget.attempts<budget.maximum_calls and budget.estimated_usd+budget.reserved_usd+RESERVE<=budget.budget_usd,'dispatch_budget_or_call_limit')
            ref=request['request_ref'];need(isinstance(ref,str) and 0<len(ref)<=80,'dispatch_request_ref')
            need(not Attempt.objects.filter(budget=budget,stage=stage,request_ref=ref).exists(),'dispatch_already_attempted')
            attempt=Attempt.objects.create(budget=budget,owner_id=self.owner_id,stage=stage,request_ref=ref,
                request_hash=digest(request['messages']),deadline_at=timezone.now()+timedelta(seconds=timeout))
            budget.attempts+=1;budget.reserved_usd+=RESERVE;budget.save(update_fields=['attempts','reserved_usd'])
            return attempt.pk
    def _settle(self,pk,response,*,late=False,unknown=False,not_dispatched=False):
        reason=None;cost=Decimal(0);usage=response.get('usage') if isinstance(response,dict) else None
        known=not unknown and isinstance(usage,dict) and all(type(usage.get(k)) is int and usage[k]>=0 for k in ['prompt_tokens','completion_tokens'])
        if not_dispatched:reason='dispatch_audit_failed';known=True;usage={}
        elif not known:reason='dispatch_transport_or_unknown_usage'
        else:
            cost=(Decimal(usage['prompt_tokens'])*Decimal('.30')+Decimal(usage['completion_tokens'])*Decimal('1.20'))/1000000
            if response.get('model')!=MODEL:reason='dispatch_model_changed'
            elif usage['prompt_tokens']>15000 or usage['completion_tokens']>1024:reason='dispatch_usage_exceeded'
            elif response.get('finish_reason')!='stop' or not isinstance(response.get('content'),str):reason='dispatch_incomplete'
            elif late:reason='dispatch_late'
        with transaction.atomic():
            Budget.objects.filter(batch_key=self.batch_key).update(lock_version=F('lock_version')+1)
            budget=Budget.objects.get(batch_key=self.batch_key);attempt=Attempt.objects.get(pk=pk,budget=budget)
            need(attempt.status=='dispatching','dispatch_already_settled')
            attempt.status='not_dispatched' if not_dispatched else 'completed' if not reason else 'failed' if known else 'unknown'
            attempt.usage=usage if known else {};attempt.estimated_usd=cost;attempt.reason=reason or ''
            attempt.response_hash=digest(response) if response is not None else '';attempt.save()
            if known:budget.reserved_usd-=RESERVE;budget.estimated_usd+=cost
            if budget.estimated_usd>budget.budget_usd:reason='dispatch_budget_exceeded'
            if reason:budget.closed=True;budget.reason=reason
            budget.save(update_fields=['reserved_usd','estimated_usd','closed','reason'])
        if reason:raise NaturalMemoryContractError(reason)
    def call(self,request,stage):
        self.check_owner(self.owner_id)
        try:
            need(callable(self.verify) and self.verify() is True,'dispatch_freeze_changed')
            need(callable(self.sink) and callable(self.counter),'dispatch_audit_and_counter_required')
            size=check_model_messages_budget(request['messages']);need(size==request['input_bytes'],'dispatch_input_bytes')
            count=self.counter(request['messages']);need(type(count) is int and 0<=count and count+8192<=15000,'dispatch_input_reserve')
            left=request.get('remaining_original_seconds');cap=request.get('constraints',{}).get('timeout_seconds_cap')
            need(type(left) in [int,float] and type(cap) in [int,float] and math.isfinite(left) and math.isfinite(cap) and left>0 and cap>0,'dispatch_deadline')
            timeout=min(45,left,cap);pk=self._reserve(request,stage,timeout)
        except Exception as error:
            close_budget(self.batch_key,str(error) if isinstance(error,NaturalMemoryContractError) else 'dispatch_preflight_failed')
            raise
        try:self.sink({'event':'dispatch','attempt_id':pk,'stage':stage,'request_ref':request['request_ref'],
            'messages':request['messages'],'input_bytes':size,'content_tokens':count,'wall_timeout_seconds':timeout})
        except Exception:
            self._settle(pk,None,not_dispatched=True);raise
        started=time.monotonic()
        try:response=(self.transport or request_once)(request['messages'],max_tokens=1024,timeout=timeout)
        except Exception as error:
            try:self.sink({'event':'unknown_outcome','attempt_id':pk,'stage':stage,'error_type':type(error).__name__})
            finally:self._settle(pk,None,unknown=True)
            raise
        try:self.sink({'event':'response','attempt_id':pk,'stage':stage,'result':response,'elapsed_seconds':time.monotonic()-started})
        except Exception:
            try:self._settle(pk,response,late=time.monotonic()-started>timeout)
            finally:close_budget(self.batch_key,'dispatch_audit_failed')
            raise NaturalMemoryContractError('dispatch_audit_failed') from None
        self._settle(pk,response,late=time.monotonic()-started>timeout)
        return response['content']
