"""Read-only production document tool: reviewed scope + BM25 gate + dense/RRF.

Only the resolved current question is embedded once. Never uploads history,
re-embeds documents, imports data, changes active versions or silently retries.
"""
import json
from functools import lru_cache
from pathlib import Path
import re
import time

from .document_library import DocumentLibrary, LibraryError, lexical_search
from .document_vectors import DocumentVectors, LocalQdrant
from .embedding import EmbeddingClient, EmbeddingConfig

SCOPES = Path(__file__).resolve().parents[2] / 'data/document_library_v1/retrieval_scopes.json'
GENERAL_TERMS = {'健康', '膳食', '饮食', '运动', '就医', '用药', '药品', '体重'}


def topic_supported(row, anchors):
    if not anchors:
        return True
    required = anchors.get(row['version_id'], [])
    text = re.sub(r'\s+', '', row['text']).lower()
    return not required or any(term.lower() in text for term in required)


@lru_cache(maxsize=1)
def production_library():
    return DocumentLibrary()


class Deadline:
    def __init__(self, seconds, clock=time.monotonic):
        self.clock, self.end = clock, clock()+seconds
    def remaining(self, cap=15):
        left = self.end-self.clock()
        if left <= 0: raise LibraryError('document_deadline')
        return min(left,cap)


class TimedQdrant(LocalQdrant):
    def __init__(self, deadline):
        super().__init__(allow_local=True)
        self.deadline = deadline
    def request(self, *args, **kwargs):
        self.timeout = self.deadline.remaining(5)
        result = super().request(*args, **kwargs)
        self.deadline.remaining()
        return result


class OneQuery:
    def __init__(self, client, deadline):
        self.client,self.deadline,self.config = client,deadline,client.config
        self.used = False
    def embed(self,texts,role):
        if self.used or role != 'query' or len(texts)!=1:
            raise LibraryError('document_query_limit')
        self.used = True
        vectors=self.client.embed(texts,role,timeout=self.deadline.remaining(15))
        if self.client.records[-1]['total_tokens'] is None:
            raise LibraryError('document_usage_unknown')
        return vectors


class DocumentSearch:
    def __init__(self, library=None, scopes_path=SCOPES):
        self._library = library
        self.scopes_path = Path(scopes_path)
        self.last_report = {'embedding_requests':0}

    @property
    def library(self):
        return self._library if self._library is not None else production_library()

    def preview(self, query, diseases=()):
        # Local deterministic admission scope; no model and no implicit corpus growth.
        if not isinstance(query,str) or not 1 <= len(query.strip()) <= 2000:
            return {'eligible':False,'reason':'question_size'}
        if re.search(r'(?:剂量|几片|(?:药|药品|药物).{0,6}吃多少|吃多少.{0,4}(?:药|片)|开处方|开药|厂家|品牌|报销|手术步骤|按压步骤|代码|算法|网页|网站|编程|程序|html|css|python|javascript)',query,re.I):
            return {'eligible':False,'reason':'outside_admitted_document_scope'}
        scopes=json.loads(self.scopes_path.read_text(encoding='utf-8'))['documents']
        active=set(self.library.active_ids())
        allowed=[]
        anchors={}
        query_lower=query.lower()
        supported=set()
        for profile in scopes:
            supported.update(profile['diseases'])
        if diseases and not set(diseases)<=supported:
            return {'eligible':False,'reason':'disease_outside_admitted_document_scope'}
        for profile in scopes:
            if (profile['version_id'] in active and any(t.lower() in query_lower for t in profile['query_terms'])
                    and (not diseases or set(diseases)&set(profile['diseases']))):
                allowed.append(profile['version_id'])
                if profile.get('doc_id') == 'nhc-health-literacy':
                    matched=[term for term in profile['query_terms'] if term.lower() in query_lower]
                    anchors[profile['version_id']]=[t for t in matched if t not in GENERAL_TERMS] or matched
        if not allowed: return {'eligible':False,'reason':'no_reviewed_scope_match'}
        candidates=[r for r in lexical_search(self.library,query,limit=15,version_ids=allowed)
                    if topic_supported(r,anchors)]
        if not candidates or candidates[0]['retrieval']['score'] < 4:
            return {'eligible':False,'reason':'weak_lexical_support'}
        return {'eligible':True,'version_ids':allowed,'topic_anchors':anchors,'candidate_count':len(candidates),
                'best_lexical_score':round(candidates[0]['retrieval']['score'],4)}

    def search(self, query, *, diseases=(), timeout=25):
        if type(timeout) not in (int,float) or not 0 < timeout <= 30:
            raise LibraryError('invalid_document_timeout')
        deadline=Deadline(timeout)
        plan=self.preview(query,diseases)
        if not plan['eligible']:
            self.last_report={'status':'no_evidence','reason':plan['reason'],'embedding_requests':0}
            return []
        ids=plan['version_ids']
        config=EmbeddingConfig.load()
        store=DocumentVectors(self.library,config.fingerprint(),TimedQdrant(deadline))
        # Readiness is checked before constructing or spending the external query.
        for version in ids:
            deadline.remaining()
            if not self.library.indexed(version,config.fingerprint(),store.collection):
                raise LibraryError('document_index_not_ready')
            store.verify_points(version)
        client=EmbeddingClient(config,allow_external=True)
        self.last_report={'status':'started','embedding_requests':0,'scope':'resolved_current_question_only'}
        try:
            # Use an explicit admitted subset; remote old/unapproved versions are filtered.
            keyword=[r for r in lexical_search(self.library,query,limit=15,version_ids=ids)
                     if topic_supported(r,plan.get('topic_anchors',{}))]
            if not keyword:
                self.last_report['status']='no_evidence'
                return []
            vector=OneQuery(client,deadline).embed([query],'query')[0]
            dense=store.search(vector,ids,limit=15)
            allowed={row['id'] for row in keyword}
            # Dense expansion is restricted to candidates with independent lexical support.
            scores,rows={},{}
            for channel in (keyword,dense):
                for rank,row in enumerate(channel,1):
                    if row['id'] not in allowed: continue
                    rows[row['id']]=row
                    scores[row['id']]=scores.get(row['id'],0)+1/(60+rank)
            selected=[]
            for key in sorted(scores,key=lambda k:(-scores[k],k)):
                row=rows[key]
                if sum(r['doc_id']==row['doc_id'] and r['page']==row['page'] for r in selected): continue
                selected.append(dict(row,retrieval={'channel':'hybrid_rrf_admitted','score':scores[key]}))
                if len(selected)==3: break
            if not set(ids)<=set(self.library.active_ids()):
                raise LibraryError('active_versions_changed_during_retrieval')
            deadline.remaining()
            self.last_report['status']='found' if selected else 'no_evidence'
            return selected
        finally:
            self.last_report['embedding_requests']=len(client.records)
            self.last_report['requests']=client.records


def document_fact(row, evidence_id):
    return {'evidence_id':evidence_id,'kind':'document','field':'document_passage',
            'disease':'','label':row['title'],'value':row['text'], 'truncated':False,
            'medical_review_status':'not_verified','sources':[{
                'kind':'document','chunk_id':row['id'],'version_id':row['version_id'],
                'doc_id':row['doc_id'],'title':row['title'],'edition':row['edition'],
                'publisher':row['publisher'],'page':row['page'],'printed_page':row['printed_page'],
                'start':row['start'],'end':row['end'],'file_sha256':row['file_sha256'],
                'text_sha256':row['text_sha256'],'source_url':row['source_url'],
                'scope_note':row['scope_note']} ]}
