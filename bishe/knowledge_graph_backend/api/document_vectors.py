"""Explicit local Qdrant adapter; an injected embedder owns authorization/budget.

No remote embedding client is constructed here. A staged version is invisible to
queries until local activation; one SQLite active-version snapshot gates both paths.
"""
import json
import math
import re
import urllib.error
import urllib.request

from .document_library import LibraryError, embedding_text, lexical_search
from .embedding import DIMENSIONS


class LocalQdrant:
    def __init__(self, *, allow_local=False, timeout=15):
        if not allow_local:
            raise LibraryError('local_qdrant_requires_opt_in')
        if type(timeout) not in (int,float) or not 0 < timeout <= 15:
            raise LibraryError('invalid_qdrant_timeout')
        self.timeout = timeout
        # No configuration-controlled remote endpoint, proxies or redirects.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise LibraryError('qdrant_redirect_refused')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None):
        raw = None if body is None else json.dumps(body, allow_nan=False).encode()
        request = urllib.request.Request('http://127.0.0.1:16333' + path, data=raw,
            headers={'Content-Type':'application/json'}, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise LibraryError('qdrant_response_too_large')
            result = json.loads(data)
            if result.get('status') != 'ok':
                raise LibraryError('qdrant_operation_failed')
            return result['result']
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise LibraryError('qdrant_not_found') from None
            raise LibraryError('qdrant_http_' + str(error.code)) from None
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            raise LibraryError('qdrant_unavailable_or_invalid_response') from None


def checked_vectors(vectors, count):
    if not isinstance(vectors, list) or len(vectors) != count:
        raise LibraryError('vector_count_mismatch')
    for vector in vectors:
        if (not isinstance(vector, list) or len(vector) != DIMENSIONS
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector)
                or abs(math.hypot(*vector) - 1) > 1e-4):
            raise LibraryError('vector_contract_mismatch')


class DocumentVectors:
    def __init__(self, library, fingerprint, qdrant):
        if not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
            raise LibraryError('invalid_embedding_fingerprint')
        self.library, self.fingerprint, self.qdrant = library, fingerprint, qdrant
        self.collection = 'medical_docs_v1_' + library.library_id()[:12] + '_' + fingerprint[:12]

    def ensure_collection(self):
        path = '/collections/' + self.collection
        try:
            result = self.qdrant.request('GET', path)
        except LibraryError as error:
            if str(error) != 'qdrant_not_found':
                raise
            self.qdrant.request('PUT', path, {'vectors':{'size':DIMENSIONS,'distance':'Cosine'}})
            result = self.qdrant.request('GET', path)
        vectors = result.get('config', {}).get('params', {}).get('vectors', {})
        if vectors != {'size':DIMENSIONS,'distance':'Cosine'}:
            # Qdrant may include defaults for optional fields; check the required ones.
            if vectors.get('size') != DIMENSIONS or vectors.get('distance') != 'Cosine':
                raise LibraryError('qdrant_collection_contract_mismatch')

    def index_version(self, version_id, embedder, *, batch_size=12):
        if type(batch_size) is not int or not 1 <= batch_size <= 20:
            raise LibraryError('invalid_batch_size')
        self.library.verify(version_id)
        if embedder.config.fingerprint() != self.fingerprint:
            raise LibraryError('embedder_fingerprint_mismatch')
        if self.library.indexed(version_id, self.fingerprint, self.collection):
            self.verify_points(version_id)
            return {'skipped':True, 'version_id':version_id}
        self.ensure_collection()
        chunks = self.library.chunks(version_id)
        for start in range(0,len(chunks),batch_size):
            batch = chunks[start:start+batch_size]
            texts = [embedding_text(self.library.evidence(c['id'],[version_id])) for c in batch]
            vectors = embedder.embed(texts, 'document')
            checked_vectors(vectors, len(batch))
            points = [{'id':c['id'],'vector':v,'payload':{
                'version_id':version_id,'fingerprint':self.fingerprint,
                'text_sha256':c['text_sha256']}} for c,v in zip(batch,vectors)]
            self.qdrant.request('PUT','/collections/'+self.collection+'/points?wait=true',{'points':points})
        # Cross-store commit: catalog readiness only after every point is read back.
        self.verify_points(version_id)
        self.library.mark_indexed(version_id, self.fingerprint, self.collection)
        return {'skipped':False,'version_id':version_id,'chunks':len(chunks)}

    def verify_points(self, version_id):
        chunks = self.library.chunks(version_id)
        for start in range(0,len(chunks),20):
            batch = chunks[start:start+20]
            rows = self.qdrant.request('POST','/collections/'+self.collection+'/points',
                {'ids':[c['id'] for c in batch],'with_payload':True,'with_vector':True})
            expected = {c['id']:c for c in batch}
            if not isinstance(rows,list) or len(rows) != len(batch) or {r['id'] for r in rows} != set(expected):
                raise LibraryError('qdrant_readback_missing_points')
            for row in rows:
                c = expected[row['id']]
                if row.get('payload') != {'version_id':version_id,'fingerprint':self.fingerprint,
                                          'text_sha256':c['text_sha256']}:
                    raise LibraryError('qdrant_readback_payload_mismatch')
                checked_vectors([row.get('vector')], 1)

    def search(self, query_vector, version_ids, limit=10):
        checked_vectors([query_vector],1)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise LibraryError('invalid_limit')
        if not version_ids:
            return []
        for version in version_ids:
            if not self.library.indexed(version, self.fingerprint, self.collection):
                raise LibraryError('active_versions_not_indexed')
        body = {'query':query_vector,'limit':limit,'with_payload':True,
                'filter':{'must':[{'key':'version_id','match':{'any':version_ids}},
                                  {'key':'fingerprint','match':{'value':self.fingerprint}}]}}
        result = self.qdrant.request('POST','/collections/'+self.collection+'/points/query',body)
        rows = []
        for point in result['points']:
            e = self.library.evidence(point['id'],version_ids)
            if point.get('payload') != {'version_id':e['version_id'],'fingerprint':self.fingerprint,
                                       'text_sha256':e['text_sha256']}:
                raise LibraryError('retrieved_payload_mismatch')
            score = point.get('score')
            if type(score) not in (float,int) or not math.isfinite(score):
                raise LibraryError('invalid_retrieval_score')
            rows.append(dict(e,retrieval={'channel':'dense','score':score}))
        return rows


def hybrid_search(library, vector_store, query, embedder, limit=5):
    # Snapshot first. Validate complete readiness before any billable query.
    if type(limit) is not int or not 1 <= limit <= 10:
        raise LibraryError('invalid_limit')
    if embedder.config.fingerprint() != vector_store.fingerprint:
        raise LibraryError('embedder_fingerprint_mismatch')
    ids = library.active_ids()
    if not ids:
        return []
    for version in ids:
        library.verify(version)
        if not library.indexed(version, vector_store.fingerprint, vector_store.collection):
            raise LibraryError('active_versions_not_indexed')
        vector_store.verify_points(version)
    keyword = lexical_search(library,query,limit=20,version_ids=ids)
    vector = embedder.embed([query],'query')[0]
    dense = vector_store.search(vector,ids,limit=20)
    scores, evidence = {}, {}
    for channel in (keyword,dense):
        for rank,row in enumerate(channel,1):
            evidence[row['id']] = row
            scores[row['id']] = scores.get(row['id'],0) + 1/(60+rank)
    result = []
    for key in sorted(scores,key=lambda key:(-scores[key],key))[:limit]:
        row = dict(evidence[key],retrieval={'channel':'hybrid_rrf','score':scores[key]})
        result.append(row)
    # If the active set changed during the request, don't return stale evidence.
    if library.active_ids() != ids:
        raise LibraryError('active_versions_changed_during_retrieval')
    return result
