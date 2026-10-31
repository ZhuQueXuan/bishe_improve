"""Local release wiring. Imports do not create budgets, read chats or send SDK calls."""
import hashlib,json,os,sys,uuid
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from django.conf import settings
from django.utils import timezone
from .natural_memory_contract import need
from .natural_memory_dispatch import BoundedModelProvider,approve_budget

ROOT=Path(__file__).resolve().parents[3]
RUNTIME=ROOT/'bishe/data/natural_memory_v1/runtime'
MANIFEST=Path(os.getenv('NATURAL_MEMORY_RELEASE_MANIFEST',str(ROOT/'bishe/data/natural_memory_v1/runtime_release_v1_1_0.json')))

def verify_release():
    record=json.loads(MANIFEST.read_text(encoding='utf-8'))
    need(record.get('ready') is True and record.get('version')=='1.1.0','release_not_ready')
    need(all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in record['source_sha256'].items()),'release_source_changed')
    return True

@lru_cache(maxsize=1)
def counter():
    path=RUNTIME/'deepseek-v41-tokenizer.json'
    need(hashlib.sha256(path.read_bytes()).hexdigest()=='81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99','tokenizer_changed')
    sys.path.insert(0,str(RUNTIME/'tokenizer_runtime'))
    from tokenizers import Tokenizer
    tokenizer=Tokenizer.from_file(str(path))
    return lambda messages:sum(len(tokenizer.encode(m['content'],add_special_tokens=False).ids) for m in messages)

def professional(question,purpose):
    if purpose=='memory_review' or not settings.DOCUMENT_RAG_ENABLED:return []
    from .document_search import DocumentSearch
    rows=DocumentSearch().search(question,timeout=20)
    return [{'ref':'E'+str(i),'title':row['title'],'text':row['text'],'source':{
        'document_id':row['doc_id'],'version_id':row['version_id'],'page':row['page'],
        'text_sha256':hashlib.sha256(row['text'].encode()).hexdigest()}} for i,row in enumerate(rows,1)]

def audit_metadata_only(event):
    # DispatchAttempt already persists request/response hashes and exact usage.
    # Never add private chat/model bodies to shared server logs.
    return None

class ReleaseProvider(BoundedModelProvider):
    def __init__(self):
        super().__init__('release-template',counter=lambda messages:counter()(messages),professional=professional,
            audit_sink=audit_metadata_only,verify_frozen=verify_release)
    def for_user(self,user):
        self._gate();verify_release()
        key='live-'+uuid.uuid4().hex
        # One operation has at most routing+answer or extraction+review. This is
        # a durable per-operation envelope, not a cumulative user/task quota.
        approve_budget(batch_key=key,authorization_hash=hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
            owner_ids=[user.pk],stages=['extraction','review','query_intent','answer'],maximum_calls=4,
            budget_cny='.24',expires_at=timezone.now()+timedelta(minutes=3))
        bound=BoundedModelProvider(key,owner_id=user.pk,counter=self.counter,professional=self.retrieve,
            audit_sink=self.sink,verify_frozen=self.verify)
        bound.check_owner(user.pk);return bound

def install():
    verify_release()
    settings.NATURAL_MEMORY_CHAT_PROVIDER=ReleaseProvider()
    settings.NATURAL_MEMORY_BACKGROUND_PROVIDER=ReleaseProvider()
