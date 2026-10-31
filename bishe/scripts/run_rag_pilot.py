"""Fixed six-case/three-mode pilot. Default is a zero-call plan; --live is explicit.

No service lifecycle operations, account writes, corpus updates or retries.
"""
import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

PROJECT=Path(__file__).resolve().parents[1]
ROOT=PROJECT.parent
sys.path.insert(0,str(PROJECT/'knowledge_graph_backend'))
from check_rag_evaluation import audit, SUITE


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    checked=audit()
    suite=json.loads(SUITE.read_text(encoding='utf-8'))
    pilot_ids=suite['run_policy']['initial_pilot_case_ids']
    questions={c['id']:{k:c[k] for k in ('id','query','history')} for c in suite['cases'] if c['id'] in pilot_ids}
    template=json.loads((SUITE.parent/'review_template.json').read_text(encoding='utf-8'))
    runs=[{k:r[k] for k in ('case_id','mode','planned_mode_order')} for r in template['results'] if r['case_id'] in pilot_ids]
    plan={'batch_id':'rag-pilot-v1-20261023','case_ids':pilot_ids,'runs':runs,
          'planned_runs':18,'max_understanding':12,'max_answers':18,'max_query_embeddings':12,
          'max_estimated_rmb':'1','automatic_retries':0,'dataset_sha256':checked['frozen_sha256'],
          'runtime_sha256':sha(Path(__file__).with_name('rag_evaluation_runtime.py')),
          'runner_sha256':sha(Path(__file__)),'medical_review_status':'not_verified',
          'phase':'pilot_only_no_automatic_expansion',
          'normalization_note':'All modes use ChatAgent local resolution; G uses DisabledDocuments to preserve the same resolved answer question as J. No actual document read in G.'}
    if not args.live:
        print(json.dumps({**plan,'external_calls':0,'status':'plan_only'},ensure_ascii=False,indent=2)); return

    os.environ['DJANGO_SETTINGS_MODULE']='knowledge_graph_backend.settings'
    import django
    django.setup()
    from django.conf import settings
    settings.DATABASES['default']['NAME']=':memory:'
    from api.utils import DeepSeekAPI
    from api.graph_retrieval import retrieve_context
    from api.fulltext_retrieval import retrieve_phrase
    from api.document_search import DocumentSearch,production_library
    from api.document_vectors import DocumentVectors,LocalQdrant
    from api.embedding import EmbeddingConfig,EmbeddingClient,NativeHTTPTransport
    from rag_evaluation_runtime import make_agent,Ledger,GuardedSDK,GuardedEmbeddingTransport,deterministic_reply,ExperimentStop
    from api.evidence_sources import public_sources

    # Local preflight before the irreversible batch claim or any paid request.
    library=production_library(); config=EmbeddingConfig.load()
    store=DocumentVectors(library,config.fingerprint(),LocalQdrant(allow_local=True))
    for version in library.active_ids():
        if not library.indexed(version,config.fingerprint(),store.collection):
            raise ExperimentStop('preflight_index_not_ready')
        store.verify_points(version)
    # Only graph health/source readiness: a fixed nonspecific read, never a model query.
    retrieve_context('资料预检',[],['desc'],timeout=10)
    model=DeepSeekAPI()
    plan['model']={'name':model.model,'max_tokens':model.max_tokens,'temperature':model.temperature,
                   'thinking':'disabled','query_max_tokens':600}
    if model.model!='deepseek-flash' or model.max_tokens!=4096:
        raise ExperimentStop('model_settings_changed')
    output=ROOT/'temp/rag_evaluation_v1/pilot_20261023'
    output.mkdir(parents=True,exist_ok=False)
    (output/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    ledger=Ledger(output/'ledger.json',allow_external=True,budget='1')
    original=model.client; model.client=GuardedSDK(original,ledger)
    protected=[PROJECT/'data/medical.json',PROJECT/'knowledge_graph_backend/db.sqlite3']
    before={str(p.relative_to(ROOT)):sha(p) for p in protected}
    live_transport=NativeHTTPTransport(allow_external=True)
    def embedder(conf,**kwargs):
        return EmbeddingClient(conf,transport=GuardedEmbeddingTransport(live_transport,ledger))
    results=[]
    report={'plan':plan,'status':'running','results':results,'protected_before':before,
            'review_status':'unreviewed','external_calls':0,'account_database':'memory','graph_access':'read_only'}
    report_path=output/'results.json'
    def save():
        report['external_calls']=len(ledger.data['calls'])
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    error=None
    try:
        with patch('api.document_search.EmbeddingClient',side_effect=embedder):
            for run in runs:
                case=questions[run['case_id']]
                run_id=case['id']+'-'+run['mode']
                ledger.current=run_id
                row={**run,'run_id':run_id,'status':'started','query':case['query'],'history':case['history'],
                     'answer':None,'retrieval':None,'technical_error':None,'scores':None}
                results.append(row); save()
                started=time.monotonic()
                try:
                    agent=make_agent(run['mode'],retrieve_context,retrieve_phrase,lambda:model,DocumentSearch(),
                        history=case['history'],query=case['query'],session_id=run_id,owner={'session':run_id,'user':None})
                    retrieval=agent.retrieve(case['query'],case['history'])
                    row['retrieval']=retrieval
                    row['resolved_answer_question']=agent.answer_query
                    if ledger.data['halted']:
                        raise ExperimentStop('batch_halted_during_retrieval')
                    if (retrieval.get('documents',{}).get('status')=='unavailable' or
                            retrieval.get('documents',{}).get('reason')=='document_service_unavailable' or
                            agent.trace.get('stop_reason')=='deadline_exceeded'):
                        raise ExperimentStop('tool_unavailable_or_deadline')
                    if retrieval['context']:
                        answer=agent.answer(case['query'],retrieval,case['history'])
                        if ledger.data['halted']: raise ExperimentStop('batch_halted_during_answer')
                        row['status']='answer_rejected' if answer is None else 'answered'
                    else:
                        answer=deterministic_reply(retrieval)
                        row['status']='no_context'
                    row.update(answer=answer,sources=public_sources(retrieval['facts']) if retrieval['context'] else [],
                               agent=agent.snapshot(),input_context_bytes=len((agent.answer_query+retrieval['context']).encode('utf-8')))
                    if row['status']=='answer_rejected':
                        # Preserve raw response in ledger; citation/format rejection is a quality outcome.
                        row['rejection_reason']=agent.trace['stop_reason']
                except Exception as exc:
                    row.update(status='technical_failure',technical_error=type(exc).__name__)
                    error=type(exc).__name__
                    if not ledger.data['halted']:
                        ledger.data.update(halted=True,halt_reason='run_technical_failure'); ledger.save()
                finally:
                    row['latency_ms']=round((time.monotonic()-started)*1000,2)
                    calls=[r for r in ledger.data['calls'] if r['run_id']==run_id]
                    row['calls']=dict(Counter(r['kind'] for r in calls))
                    row['provider_usage']=[{'kind':r['kind'],'usage':r['usage'],'status':r['status']} for r in calls]
                    row['estimated_rmb']=str(sum((Decimal(r.get('estimated_rmb',r['reserved_rmb'])) for r in calls),Decimal(0)))
                    save()
                    print(json.dumps({'run_id':run_id,'status':row['status'],'calls':row['calls'],'total_calls':len(ledger.data['calls'])},ensure_ascii=False),flush=True)
                if error or ledger.data['halted']: break
    finally:
        original.close()
        after={str(p.relative_to(ROOT)):sha(p) for p in protected}
        report.update(status='completed' if not error and len(results)==18 and not ledger.data['halted'] else 'stopped',
            error_type=error,protected_after=after,protected_unchanged=before==after,
            estimated_rmb=str(ledger.liability()),calls_by_kind=dict(Counter(r['kind'] for r in ledger.data['calls'])),
            unattempted=[r['case_id']+'-'+r['mode'] for r in runs if r['case_id']+'-'+r['mode'] not in {x['run_id'] for x in results}],
            scope='fixed developer-visible sample; no medical or generalisation claims')
        ledger.close(report['status']); save()
    print(json.dumps({k:v for k,v in report.items() if k in {'status','error_type','protected_unchanged','estimated_rmb','calls_by_kind','external_calls','unattempted'}},ensure_ascii=False),flush=True)
    if report['status']!='completed' or before!=after: sys.exit(1)


if __name__=='__main__': main()
