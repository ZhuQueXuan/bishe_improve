"""Explicitly authorized experiment transport and durable batch cost ledger.

Not used by production views. CLI must explicitly opt in; no hidden retries.
"""
import json
import time
from decimal import Decimal
from pathlib import Path
from .deepseek_planner import DeepSeekPlanner,MODEL,usage_record
from .planning_runtime import BoundaryError

TASKS=('P01','P02','P05','P07')
# Official peak USD prices, conservative RMB budget conversion (not invoice FX).
INPUT=Decimal('0.30'); HIT=Decimal('0.006'); OUTPUT=Decimal('1.20'); FX=Decimal('8')
RESERVE=(Decimal(13024)*INPUT+Decimal(300)*OUTPUT)/Decimal(1000000)*FX


class BatchLedger:
    def __init__(self,path,temp_root):
        self.path=Path(path).resolve()
        if not self.path.is_relative_to(Path(temp_root).resolve()) or self.path.exists():raise BoundaryError('ledger_path')
        self.data={'version':'authorized_planner_batch_v1','max_calls':16,'budget_rmb':'1',
                   'price_basis':'2026-10-04 official peak USD, FX budget coefficient 8',
                   'halted':False,'halt_reason':'','calls':[]}
        with self.path.open('x',encoding='utf-8') as f:json.dump(self.data,f,ensure_ascii=False,indent=2)
    def save(self):
        with self.path.open('w',encoding='utf-8') as f:json.dump(self.data,f,ensure_ascii=False,indent=2)
    def reserve(self,task_id):
        calls=self.data['calls']
        liability=sum(Decimal(x.get('estimated_rmb',x['reserved_rmb'])) for x in calls)
        if (self.data['halted'] or task_id not in TASKS or len(calls)>=16
                or sum(x['task_id']==task_id for x in calls)>=4 or liability+RESERVE>Decimal('1')):
            raise BoundaryError('batch_budget_or_halted')
        entry={'number':len(calls)+1,'task_id':task_id,'reserved_rmb':str(RESERVE),'status':'reserved','usage':None}
        calls.append(entry);self.save();return entry
    def halt(self,reason):
        self.data['halted']=True;self.data['halt_reason']=reason;self.save()
    def settle(self,entry,usage):
        entry['usage']=usage
        if usage is None:
            entry['status']='usage_unknown';self.halt('usage_unknown');return
        hit=usage.get('prompt_cache_hit_tokens',0)
        if not 0<=hit<=usage['prompt_tokens']:
            entry['status']='usage_invalid';self.halt('usage_invalid');return
        miss=usage['prompt_tokens']-hit
        cost=(Decimal(hit)*HIT+Decimal(miss)*INPUT+Decimal(usage['completion_tokens'])*OUTPUT)/Decimal(1000000)*FX
        entry.update(status='settled',estimated_rmb=str(cost))
        if (usage['prompt_tokens']>13024 or usage['completion_tokens']>300 or cost>RESERVE):
            self.halt('reservation_exceeded')
        else:self.save()


class AuthorizedTransport:
    label='authorized_live_sdk'
    def __init__(self,client,ledger):self.client,self.ledger,self.task_id=client,ledger,None
    def complete(self,**kwargs):
        if (kwargs.get('model')!=MODEL or kwargs.get('max_tokens')!=300 or kwargs.get('stream') is not False
                or kwargs.get('extra_body')!={'thinking':{'type':'disabled'}}
                or not 0<kwargs.get('timeout',0)<=15
                or sum(len(m['content'].encode('utf-8')) for m in kwargs['messages'])>12000):
            raise BoundaryError('authorized_request_contract')
        entry=self.ledger.reserve(self.task_id)
        try:
            response=self.client.with_options(max_retries=0).chat.completions.create(**kwargs)
        except Exception:
            entry['status']='transport_failed_usage_unknown';self.ledger.halt('transport_failed_usage_unknown')
            raise BoundaryError('authorized_transport_error') from None
        self.ledger.settle(entry,usage_record(getattr(response,'usage',None)))
        return response


class AuthorizedPlanner(DeepSeekPlanner):
    local_only=False
    def __init__(self,transport,clock=time.monotonic):
        if type(transport) is not AuthorizedTransport:raise BoundaryError('authorized_transport_required')
        self.transport,self.clock,self.records=transport,clock,[]
