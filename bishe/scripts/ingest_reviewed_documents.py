"""Incremental admission of reviewed PDF ranges. Default: preview, zero writes/calls."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

PROJECT=Path(__file__).resolve().parents[1]
ROOT=PROJECT.parent
DATA=PROJECT/'data/document_library_v1'
sys.path.insert(0,str(PROJECT/'knowledge_graph_backend'))
from api.document_library import DocumentLibrary, LibraryError, canonical, embedding_text, inspect_pdf, lexical_search
from api.document_vectors import DocumentVectors, LocalQdrant, hybrid_search
from api.embedding import EmbeddingClient, EmbeddingConfig, EmbeddingError
from index_documents import BudgetedEmbedder, CachedQueries, estimate_allowance, PRICE_PER_MILLION

BUDGET=Decimal('0.15')
REVIEW=DATA/'admission_review_20261023.json'


def batch_hash(texts,role):
    return hashlib.sha256(canonical({'texts':texts,'role':role}).encode()).hexdigest()


class PlannedEmbedder(BudgetedEmbedder):
    def __init__(self,*args,allowed_batches,**kwargs):
        super().__init__(*args,**kwargs)
        self.allowed_batches=allowed_batches
    def embed(self,texts,role):
        position=len(self.client.records)
        if position>=len(self.allowed_batches) or batch_hash(texts,role)!=self.allowed_batches[position]:
            self.stopped=True
            raise EmbeddingError('request_outside_reviewed_scope')
        return super().embed(texts,role)


def prepare(library,config):
    raw=REVIEW.read_bytes()
    review=json.loads(raw)
    if review.get('review_id')!='chinese-admission-20261023':
        raise LibraryError('unknown_review')
    class NoNetwork:
        def request(self,*args): raise AssertionError('preview attempted network')
    store=DocumentVectors(library,config.fingerprint(),NoNetwork())
    admitted=[]
    batches=[]
    for item in review['documents']:
        if item['decision']=='deferred': continue
        if item['decision']!='approved_selected_text': raise LibraryError('invalid_admission_decision')
        metadata_path=(DATA/item['metadata_file']).resolve()
        candidate=(ROOT/item['candidate_file']).resolve()
        if (not metadata_path.is_relative_to((DATA/'metadata').resolve())
                or not candidate.is_relative_to((ROOT/'temp/documents/chinese-candidates-20261023/originals').resolve())):
            raise LibraryError('review_path_outside_allowed_source_roots')
        meta=json.loads(metadata_path.read_text(encoding='utf-8'))
        prepared=inspect_pdf(candidate,meta)
        if (prepared['version_id']!=item['version_id'] or meta['sha256']!=item['candidate_sha256']
                or len(prepared['chunks'])!=item['chunks']):
            raise LibraryError('reviewed_source_changed')
        current=next((v for v in library.versions() if v['id']==prepared['version_id']),None)
        ready=bool(current and library.indexed(prepared['version_id'],config.fingerprint(),store.collection))
        texts=[embedding_text(dict(c,title=meta['title'],edition=meta['edition'])) for c in prepared['chunks']]
        if not ready:
            batches.extend((texts[i:i+12],'document') for i in range(0,len(texts),12))
        admitted.append({'item':item,'metadata':meta,'candidate':candidate,'texts':texts,'ready':ready})
    cases=list(review['cases'])
    # Recheck three old development queries without regenerating any old document vectors.
    old=json.loads((DATA/'retrieval_dev_cases.json').read_text(encoding='utf-8'))
    cases.extend(old[i] for i in (0,7,9))
    batches.append(([c['query'] for c in cases],'query'))
    allowance=sum(estimate_allowance(t,role) for t,role in batches)
    cost=Decimal(allowance)*PRICE_PER_MILLION/1_000_000
    if not admitted or len(batches)>20 or cost>BUDGET:
        raise LibraryError('admission_batch_exceeds_small_usage_limit')
    baseline=library.active_ids()
    scope={'review_sha256':hashlib.sha256(raw).hexdigest(),'baseline_active':baseline,
           'sources':[{'metadata':a['metadata'],'texts':a['texts']} for a in admitted],
           'cases':cases,'fingerprint':config.fingerprint()}
    plan={'scope_sha256':hashlib.sha256(canonical(scope).encode()).hexdigest(),
          'review_id':review['review_id'],'review_sha256':scope['review_sha256'],
          'baseline_active':baseline,'collection':store.collection,'fingerprint':config.fingerprint(),
          'admitted':[{'doc_id':a['metadata']['doc_id'],'title':a['metadata']['title'],
                       'version_id':a['item']['version_id'],'chunks':len(a['texts']),
                       'selected_pages':a['metadata']['selected_pages'],'already_indexed':a['ready']}
                      for a in admitted],
          'deferred':[{'title':d['title'],'reason':d['reason']} for d in review['documents'] if d['decision']=='deferred'],
          'new_chunks':sum(len(a['texts']) for a in admitted if not a['ready']),
          'document_utf8_bytes':sum(len(t.encode()) for a in admitted if not a['ready'] for t in a['texts']),
          'queries':len(cases),'planned_requests':len(batches),'max_requests':len(batches),
          'budget_yuan':str(BUDGET),'conservative_estimated_cost_yuan':str(cost),
          'request_batches':[{'role':role,'items':len(t)} for t,role in batches],
          'medical_review_status':'not_verified','formal_chat_connected':False}
    return plan,admitted,cases,[batch_hash(t,role) for t,role in batches]


def execute(args,library,config,plan,admitted,cases,allowed):
    target=args.report.resolve()
    if target.exists() or not target.is_relative_to((ROOT/'temp').resolve()):
        raise LibraryError('new_report_under_project_temp_required')
    if args.expected_scope!=plan['scope_sha256'] or not args.authorization_label:
        raise LibraryError('reviewed_scope_and_authorization_label_required')
    target.parent.mkdir(parents=True,exist_ok=True)
    report={'started_at':datetime.now(timezone.utc).isoformat(),'plan':plan,
            'authorization_label':args.authorization_label,'status':'started','requests':[],
            'admission_results':[],'results':[]}
    with target.open('x',encoding='utf-8') as f: json.dump(report,f,ensure_ascii=False,indent=2)
    def save(): target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    client=EmbeddingClient(config,allow_external=True)
    bounded=PlannedEmbedder(client,report,save,max_requests=plan['max_requests'],budget=BUDGET,allowed_batches=allowed)
    store=DocumentVectors(library,config.fingerprint(),LocalQdrant(allow_local=True))
    try:
        store.ensure_collection()
        if library.active_ids()!=plan['baseline_active']: raise LibraryError('active_set_changed_before_import')
        # Consistent local catalog backup before adding records, no existing file overwritten.
        backup=target.parent/(target.stem+'-backup')
        backup.mkdir(exist_ok=False)
        with sqlite3.connect(str(library.database)) as source:
            with sqlite3.connect(str(backup/'catalog.sqlite3')) as dest: source.backup(dest)
        shutil.copy2(REVIEW,backup/'admission_review.json')
        report['catalog_backup']=str(backup/'catalog.sqlite3')
        # Snapshot is local to Qdrant and keeps the pre-import vector collection recoverable.
        report['qdrant_snapshot']=store.qdrant.request('POST','/collections/'+store.collection+'/snapshots')
        save()
        for source in admitted:
            imported=library.import_pdf(source['candidate'],source['metadata'])
            result=store.index_version(imported['version_id'],bounded)
            library.activate(imported['version_id'])  # only after all new points are verified
            report['admission_results'].append(dict(imported,doc_id=source['metadata']['doc_id'],
                                                     index_result=result,active=True))
            save()
        queries=[c['query'] for c in cases]
        vectors=bounded.embed(queries,'query')
        cached=CachedQueries(config,queries,vectors)
        ids=library.active_ids()
        for case,vector in zip(cases,vectors):
            paths={'lexical':lexical_search(library,case['query'],5,version_ids=ids),
                   'dense':store.search(vector,ids,5),
                   'hybrid':hybrid_search(library,store,case['query'],cached)}
            channels={}
            for channel,rows in paths.items():
                rank=next((n+1 for n,r in enumerate(rows) if r['doc_id']==case['doc_id'] and r['page'] in case['pages']),None)
                channels[channel]={'first_expected_page_rank':rank,'top5':[
                    {k:r[k] for k in ('id','doc_id','version_id','page','text_sha256')} for r in rows]}
            report['results'].append({'case_id':case['id'],'retrieval':channels})
            save()
        report['status']='completed'
    except Exception as error:
        report['status']='stopped'
        report['reason']=str(error) if isinstance(error,(LibraryError,EmbeddingError)) else type(error).__name__
    finally:
        report['requests']=client.records
        report.pop('pending',None)
        tokens=[r['total_tokens'] for r in client.records]
        report['usage_complete']=all(v is not None for v in tokens)
        report['known_tokens']=sum(v for v in tokens if v is not None)
        report['known_estimated_cost_yuan']=str(Decimal(report['known_tokens'])*PRICE_PER_MILLION/1_000_000)
        report['batch_ended_no_automatic_retry']=True
        report['ended_at']=datetime.now(timezone.utc).isoformat()
        save()
    return {k:report[k] for k in ('status','known_tokens','known_estimated_cost_yuan','usage_complete')}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-reviewed',action='store_true')
    parser.add_argument('--expected-scope')
    parser.add_argument('--authorization-label')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    try:
        library=DocumentLibrary(); config=EmbeddingConfig.load()
        plan,admitted,cases,allowed=prepare(library,config)
        if not args.execute_reviewed:
            print(json.dumps({'external_model_calls':0,'formal_writes':False,'plan':plan},ensure_ascii=False,indent=2))
            return 0
        if args.report is None: parser.error('A new report is required.')
        result=execute(args,library,config,plan,admitted,cases,allowed)
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result['status']=='completed' else 1
    except (LibraryError,EmbeddingError,OSError,ValueError) as error:
        print(json.dumps({'status':'stopped','reason':str(error)},ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())
