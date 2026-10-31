"""Default is a zero-call plan. Execution needs a new explicitly authorized batch."""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'knowledge_graph_backend'))
from api.document_library import DocumentLibrary, LibraryError, canonical, embedding_text, lexical_search
from api.document_vectors import DocumentVectors, LocalQdrant, hybrid_search
from api.embedding import EmbeddingClient, EmbeddingConfig, EmbeddingError, build_payload

DATA = PROJECT/'data/document_library_v1'
PRICE_PER_MILLION = Decimal('0.5')
BUDGET = Decimal('0.05')
MAX_REQUESTS = 5


def estimate_allowance(texts,role):
    payload = build_payload(texts,role)
    # Local conservative estimate, not a provider-side hard billing cap.
    return len(json.dumps(payload,ensure_ascii=False).encode('utf-8'))+1024


class BudgetedEmbedder:
    def __init__(self, client, report, save, *, max_requests=MAX_REQUESTS, budget=BUDGET):
        if (type(max_requests) is not int or not 1 <= max_requests <= 20
                or not isinstance(budget,Decimal) or not budget.is_finite() or not Decimal('0') < budget <= Decimal('0.20')):
            raise EmbeddingError('invalid_batch_limits')
        self.client, self.report, self.save = client, report, save
        self.config = client.config
        self.stopped = False
        self.max_requests,self.budget = max_requests,budget

    def embed(self,texts,role):
        if self.stopped:
            raise EmbeddingError('batch_already_stopped')
        try:
            if len(self.client.records) >= self.max_requests:
                raise EmbeddingError('request_limit')
            known = [r['total_tokens'] for r in self.client.records]
            if any(value is None for value in known):
                raise EmbeddingError('usage_unknown_stop')
            allowance = estimate_allowance(texts,role)
            if Decimal(sum(known)+allowance)*PRICE_PER_MILLION/1_000_000 > self.budget:
                raise EmbeddingError('estimated_budget_exhausted')
            self.report.update(status='request_pending',pending={'role':role,'items':len(texts)})
            self.save()
            vectors = self.client.embed(texts,role)
            self.report['requests'] = self.client.records
            self.report.pop('pending',None)
            self.save()
            if self.client.records[-1]['total_tokens'] is None:
                raise EmbeddingError('usage_unknown_stop')
            return vectors
        except Exception:
            self.stopped = True
            self.report['requests'] = self.client.records
            self.report['status'] = 'stopped'
            self.save()
            raise


class CachedQueries:
    def __init__(self,config,queries,vectors):
        self.config = config
        self.cache = dict(zip(queries,vectors))
    def embed(self,texts,role):
        if role != 'query' or any(text not in self.cache for text in texts):
            raise LibraryError('query_outside_frozen_batch')
        return [self.cache[text] for text in texts]


def prepare(library, config):
    manifest = json.loads((DATA/'seed_manifest.json').read_text(encoding='utf-8'))
    cases = json.loads((DATA/'retrieval_dev_cases.json').read_text(encoding='utf-8'))
    ids = [m['version_id'] for m in manifest]
    if set(library.active_ids()) != set(ids):
        raise LibraryError('active_set_differs_from_frozen_seed')
    # Identity is computed without opening a network transport.
    class NoNetwork:
        def request(self,*args): raise AssertionError('planning must not access Qdrant')
    store = DocumentVectors(library,config.fingerprint(),NoNetwork())
    texts, batches = {}, []
    for version in ids:
        library.verify(version)
        rows = library.chunks(version)
        texts[version] = [embedding_text(library.evidence(c['id'],[version])) for c in rows]
        if not library.indexed(version,config.fingerprint(),store.collection):
            batches.extend((texts[version][i:i+12],'document') for i in range(0,len(rows),12))
    queries = [case['query'] for case in cases]
    batches.append((queries,'query'))
    allowance = sum(estimate_allowance(t,role) for t,role in batches)
    if len(batches)>MAX_REQUESTS or Decimal(allowance)*PRICE_PER_MILLION/1_000_000>BUDGET:
        raise LibraryError('frozen_scope_exceeds_batch_limits')
    scope = {'versions':ids,'document_texts':texts,'queries':queries,
             'fingerprint':config.fingerprint()}
    plan = {'scope_sha256':hashlib.sha256(canonical(scope).encode()).hexdigest(),
            'version_ids':ids,'documents':len(ids),'chunks':sum(len(x) for x in texts.values()),
            'document_embedding_utf8_bytes':sum(len(t.encode()) for values in texts.values() for t in values),
            'development_queries':len(queries),'planned_requests':len(batches),
            'request_batches':[{'role':role,'items':len(t)} for t,role in batches],
            'max_requests':MAX_REQUESTS,'budget_yuan':str(BUDGET),
            'conservative_estimated_cost_yuan':str(Decimal(allowance)*PRICE_PER_MILLION/1_000_000),
            'collection':store.collection,'config_fingerprint':config.fingerprint(),
            'scope':'public_seed_documents_and_authored_development_queries_only',
            'not_sent':['chat_history','user_accounts','original_medical_json','neo4j_records'],
            'evaluation':'development_retrieval_only_no_answer_model_or_medical_validation'}
    return plan,cases


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-authorized',action='store_true')
    parser.add_argument('--approval-label')
    parser.add_argument('--expected-scope')
    parser.add_argument('--report',type=Path)
    args = parser.parse_args()
    library = DocumentLibrary()
    try:
        config = EmbeddingConfig.load()
        plan,cases = prepare(library,config)
    except (LibraryError,EmbeddingError,OSError,ValueError) as error:
        print(json.dumps({'ok':False,'error':str(error),'external_model_calls':0}))
        return 1
    if not args.execute_authorized:
        print(json.dumps({'ok':True,'external_model_calls':0,'plan':plan},ensure_ascii=False,indent=2))
        return 0
    if (not args.approval_label or len(args.approval_label)>100
            or args.expected_scope != plan['scope_sha256'] or args.report is None):
        parser.error('A new batch approval label, exact reviewed scope hash and report are required.')
    target = args.report.resolve()
    if target.exists() or not target.is_relative_to((PROJECT.parent/'temp').resolve()):
        parser.error('Use a new report under project temp.')
    target.parent.mkdir(parents=True,exist_ok=True)
    report = {'started_at':datetime.now(timezone.utc).isoformat(),'approval_label':args.approval_label,
              'plan':plan,'requests':[],'status':'started'}
    with target.open('x',encoding='utf-8') as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2)
    def save():
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    client = EmbeddingClient(config,allow_external=True)
    bounded = BudgetedEmbedder(client,report,save)
    store = DocumentVectors(library,config.fingerprint(),LocalQdrant(allow_local=True))
    try:
        store.ensure_collection()  # fail before a model call if local DB is unavailable
        report['index_results'] = []
        for version in plan['version_ids']:
            report['index_results'].append(store.index_version(version,bounded))
            save()
        queries = [case['query'] for case in cases]
        vectors = bounded.embed(queries,'query')
        cached = CachedQueries(config,queries,vectors)
        report['results'] = []
        ids = library.active_ids()
        for case,vector in zip(cases,vectors):
            paths = {'lexical':lexical_search(library,case['query'],5,version_ids=ids),
                     'dense':store.search(vector,ids,limit=5),
                     'hybrid':hybrid_search(library,store,case['query'],cached)}
            results = {}
            for channel,rows in paths.items():
                rank = next((i+1 for i,r in enumerate(rows)
                             if r['doc_id']==case['doc_id'] and r['page'] in case['pages']),None)
                results[channel] = {'first_relevant_rank':rank,'top5':[
                    {k:r[k] for k in ('id','doc_id','version_id','page','text_sha256')} for r in rows]}
            report['results'].append({'case_id':case['id'],'retrieval':results})
            save()
        report['status'] = 'completed'
    except (EmbeddingError,LibraryError,OSError,ValueError,KeyError,TypeError) as error:
        report['status'] = 'stopped'
        report['reason'] = str(error) if isinstance(error,(EmbeddingError,LibraryError)) else type(error).__name__
    finally:
        report['requests'] = client.records
        report.pop('pending',None)
        usage = [r['total_tokens'] for r in client.records]
        report['usage_complete'] = all(t is not None for t in usage)
        report['known_tokens'] = sum(t for t in usage if t is not None)
        report['known_estimated_cost_yuan'] = str(Decimal(report['known_tokens'])*PRICE_PER_MILLION/1_000_000)
        report['ended_at'] = datetime.now(timezone.utc).isoformat()
        report['approval_batch_ended_no_automatic_retry'] = True
        save()
    print(json.dumps({k:v for k,v in report.items() if k in ('status','reason','known_tokens',
                     'known_estimated_cost_yuan','usage_complete')},ensure_ascii=False))
    return 0 if report['status']=='completed' else 1


if __name__=='__main__':
    raise SystemExit(main())
