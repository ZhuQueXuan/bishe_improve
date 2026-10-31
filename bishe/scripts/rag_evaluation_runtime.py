"""Source-restricted adapters and create-only budget ledger for fixed RAG experiments.

Not imported by production views. Does not change Django settings, corpus or indexes.
"""
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

from api.chat_runtime import ChatAgent
from api.agent_loop import AgentStop
from api.deepseek_planner import usage_record


class ExperimentStop(ValueError):
    pass


class DisabledDocuments:
    """Keep the same current-question normalization, with no document access."""
    last_report = {'status':'disabled', 'embedding_requests':0}
    def preview(self, query, diseases=()):
        return {'eligible':False, 'reason':'experiment_graph_only'}
    def search(self, *args, **kwargs):
        raise ExperimentStop('graph_mode_cannot_read_documents')


class AdmittedDocuments:
    """Reuse the production document-only branch with the actual disease scope."""
    def __init__(self, tool, plan, diseases):
        self.tool, self.plan, self.diseases = tool, plan, diseases
    def preview(self, *args, **kwargs):
        return self.plan
    @property
    def last_report(self):
        return self.tool.last_report
    def search(self, query, *, timeout, **kwargs):
        return self.tool.search(query, diseases=self.diseases, timeout=timeout)


class DocumentOnlyAgent(ChatAgent):
    def _with_documents(self, query, seeds, history):
        # ChatAgent.retrieve already resolved the same local session intent as G/J.
        try:
            self.remaining()
            plan = self.document_search.preview(query, seeds)
            self.remaining()
        except AgentStop:
            return self._finish([], 'deadline_exceeded')
        except Exception:
            result = self._finish([], 'document_service_unavailable')
            result['documents'] = {'status':'unavailable', 'embedding_requests':0}
            return result
        if not plan['eligible']:
            result = self._finish([], 'document_scope_no_evidence')
            result['documents'] = {'status':'skipped', 'reason':plan['reason'], 'embedding_requests':0}
            return result
        original = self.document_search
        self.document_search = AdmittedDocuments(original, plan, seeds)
        try:
            # Empty graph seeds select the existing document-only renderer/tool branch.
            # The wrapper retains the original disease allowlist for actual searches.
            return super()._with_documents(query, [], history)
        finally:
            self.document_search = original


def forbidden_graph(*args, **kwargs):
    raise ExperimentStop('document_mode_cannot_read_graph')


def make_agent(mode, graph, phrase, model_factory, document_tool, **kwargs):
    if mode == 'G':
        return ChatAgent(graph, phrase, model_factory, document_search=DisabledDocuments(), **kwargs)
    if mode == 'D':
        return DocumentOnlyAgent(forbidden_graph, forbidden_graph, model_factory,
                                 document_search=document_tool, **kwargs)
    if mode == 'J':
        return ChatAgent(graph, phrase, model_factory, document_search=document_tool, **kwargs)
    raise ExperimentStop('unknown_mode')


def deterministic_reply(retrieval):
    return {'ambiguous':'问题涉及多个疾病，请明确希望查询哪一种。',
            'nonmedical':'当前仅支持医疗知识资料查询，请输入相关问题。',
            'unsupported_field':'当前图谱未收录经过核验的药品商品名、厂家或品牌资料，无法据此回答。'}.get(
        retrieval['status'], '现有资料不足以完整回答这个问题。请补充具体疾病名称或缩小问题范围；不能据此提供诊断或用药建议。')


class Ledger:
    # Existing project's conservative accounting coefficients, not a current price quote.
    INPUT = Decimal('0.30')
    OUTPUT = Decimal('1.20')
    HIT = Decimal('0.006')
    FX = Decimal('8')
    EMBEDDING = Decimal('0.5')
    LIMITS = {'understand':12, 'answer':18, 'embedding':12}

    def __init__(self, path, *, allow_external=False, budget='1'):
        self.path = Path(path)
        self.allowed = allow_external
        self.current = None
        self.data = {'version':'rag_pilot_ledger_v1','status':'open','halted':False,'halt_reason':None,
                     'budget_rmb':str(budget),'limits':dict(self.LIMITS),'calls':[],
                     'cost_basis':'project historical conservative USD peak coefficients, FX=8; embedding RMB0.5/million; estimate not invoice',
                     'max_retries':0}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('x', encoding='utf-8') as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=2)

    def save(self):
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')

    def halt(self, reason):
        self.data.update(halted=True, halt_reason=reason)
        self.save()
        raise ExperimentStop(reason)

    def liability(self):
        return sum((Decimal(r.get('estimated_rmb', r['reserved_rmb'])) for r in self.data['calls']), Decimal(0))

    def reserve(self, kind, input_bytes, max_tokens=0):
        if not self.allowed or self.current is None or self.data['halted'] or self.data['status'] != 'open':
            raise ExperimentStop('external_batch_not_active')
        if kind not in self.LIMITS or type(input_bytes) is not int or not 0 < input_bytes <= 40000:
            self.halt('request_contract')
        if ((self.current.endswith('-G') and kind == 'embedding') or
                (self.current.endswith('-D') and kind == 'understand')):
            self.halt('source_mode_call_violation')
        counts = Counter(r['kind'] for r in self.data['calls'])
        if counts[kind] >= self.LIMITS[kind] or any(r['run_id']==self.current and r['kind']==kind for r in self.data['calls']):
            self.halt('call_limit')
        reserve = ((Decimal(input_bytes+1024)*self.INPUT + Decimal(max_tokens)*self.OUTPUT)*self.FX
                   if kind != 'embedding' else Decimal(input_bytes+256)*self.EMBEDDING) / Decimal(1000000)
        if self.liability() + reserve > Decimal(self.data['budget_rmb']):
            self.halt('cost_reservation_limit')
        row = {'number':len(self.data['calls'])+1,'run_id':self.current,'kind':kind,
               'status':'reserved','input_bytes':input_bytes,'max_tokens':max_tokens,
               'reserved_rmb':str(reserve),'usage':None}
        self.data['calls'].append(row)
        self.save()
        return row

    def settle(self, row, usage):
        row['usage'] = usage
        if not isinstance(usage, dict):
            row['status'] = 'usage_unknown'
            self.halt('usage_unknown')
        if row['kind'] == 'embedding':
            total = usage.get('total_tokens')
            if type(total) is not int or total <= 0:
                self.halt('usage_unknown')
            cost = Decimal(total)*self.EMBEDDING/Decimal(1000000)
            valid = total <= row['input_bytes']+256
        else:
            prompt, completion = usage['prompt_tokens'], usage['completion_tokens']
            hit = usage.get('prompt_cache_hit_tokens', 0)
            cost = (Decimal(hit)*self.HIT + Decimal(prompt-hit)*self.INPUT + Decimal(completion)*self.OUTPUT)*self.FX/Decimal(1000000)
            valid = 0 <= hit <= prompt <= row['input_bytes']+1024 and completion <= row['max_tokens']
        row.update(status='settled', estimated_rmb=str(cost))
        self.save()
        if not valid or cost > Decimal(row['reserved_rmb']):
            self.halt('usage_exceeds_reservation')

    def close(self, status):
        self.data['status'] = status
        self.save()


class GuardedSDK:
    def __init__(self, client, ledger):
        self.client, self.ledger = client, ledger
        self.chat = SimpleNamespace(completions=self)
    def with_options(self, **kwargs):
        if kwargs != {'max_retries':0}:
            self.ledger.halt('retries_disabled')
        return self
    def create(self, **kwargs):
        cap = kwargs.get('max_tokens')
        if (kwargs.get('model') != 'deepseek-flash' or kwargs.get('stream') is not False or
                cap not in (600,4096) or not 0 < kwargs.get('timeout',0) <= 60 or
                kwargs.get('extra_body') != {'thinking':{'type':'disabled'}}):
            self.ledger.halt('sdk_contract')
        raw = json.dumps(kwargs['messages'], ensure_ascii=False).encode('utf-8')
        kind = 'understand' if cap == 600 else 'answer'
        row = self.ledger.reserve(kind, len(raw), cap)
        row['input_sha256'] = hashlib.sha256(raw).hexdigest()
        self.ledger.save()
        start = time.monotonic()
        try:
            response = self.client.with_options(max_retries=0).chat.completions.create(**kwargs)
        except Exception:
            row['status'] = 'transport_failure_usage_unknown'
            self.ledger.halt('model_transport_failure')
        finally:
            row['latency_ms'] = round((time.monotonic()-start)*1000,2)
        self.ledger.settle(row, usage_record(getattr(response,'usage',None)))
        finish = response.choices[0].finish_reason if response.choices else None
        row['finish_reason'] = finish
        row['response_text'] = response.choices[0].message.content if response.choices else None
        self.ledger.save()
        if finish != 'stop':
            self.ledger.halt('incomplete_model_response')
        return response


class GuardedEmbeddingTransport:
    def __init__(self, transport, ledger):
        self.transport, self.ledger = transport, ledger
    def post(self, config, payload, timeout):
        texts = payload.get('input',{}).get('texts',[])
        if len(texts)!=1 or payload.get('parameters',{}).get('text_type')!='query':
            self.ledger.halt('embedding_scope')
        row = self.ledger.reserve('embedding',len(texts[0].encode('utf-8')))
        row['input_sha256'] = hashlib.sha256(texts[0].encode()).hexdigest()
        self.ledger.save()
        start = time.monotonic()
        try:
            response = self.transport.post(config,payload,timeout)
        except Exception:
            row['status'] = 'transport_failure_usage_unknown'
            self.ledger.halt('embedding_transport_failure')
        finally:
            row['latency_ms'] = round((time.monotonic()-start)*1000,2)
        self.ledger.settle(row,response.get('usage'))
        return response
