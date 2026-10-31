"""Batch guard tests; fake client only, no live configuration or network."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from index_documents import BudgetedEmbedder, CachedQueries, MAX_REQUESTS
from api.embedding import EmbeddingError
from api.document_library import LibraryError
from ingest_reviewed_documents import PlannedEmbedder, batch_hash


class FakeClient:
    config = object()
    def __init__(self, tokens=1, failure=False):
        self.records = []
        self.tokens, self.failure = tokens,failure
    def embed(self,texts,role):
        self.records.append({'total_tokens':self.tokens,'role':role})
        if self.failure: raise EmbeddingError('synthetic_failure')
        return [[1.0]+[0.0]*1023 for _ in texts]


class BudgetTests(unittest.TestCase):
    def guard(self,client):
        self.report,self.saves = {},[]
        return BudgetedEmbedder(client,self.report,lambda:self.saves.append(dict(self.report)))
    def test_request_cap_blocks_before_next_call(self):
        client = FakeClient()
        guard = self.guard(client)
        for _ in range(MAX_REQUESTS): guard.embed(['synthetic'],'document')
        with self.assertRaisesRegex(EmbeddingError,'request_limit'): guard.embed(['synthetic'],'query')
        self.assertEqual(len(client.records),MAX_REQUESTS)
    def test_unknown_usage_stops_and_does_not_retry(self):
        client = FakeClient(tokens=None)
        guard = self.guard(client)
        with self.assertRaisesRegex(EmbeddingError,'unknown'): guard.embed(['synthetic'],'document')
        with self.assertRaisesRegex(EmbeddingError,'already_stopped'): guard.embed(['synthetic'],'query')
        self.assertEqual(len(client.records),1)
    def test_first_failure_ends_batch(self):
        client = FakeClient(failure=True)
        guard = self.guard(client)
        with self.assertRaises(EmbeddingError): guard.embed(['synthetic'],'document')
        with self.assertRaisesRegex(EmbeddingError,'already_stopped'): guard.embed(['synthetic'],'document')
        self.assertEqual(len(client.records),1)
    def test_budget_exhaustion_prevents_call(self):
        client = FakeClient(tokens=99999)
        client.records.append({'total_tokens':99999})
        guard = self.guard(client)
        with self.assertRaisesRegex(EmbeddingError,'budget_exhausted'): guard.embed(['synthetic'],'document')
        self.assertEqual(len(client.records),1)
    def test_reservation_saved_before_provider_call(self):
        client = FakeClient()
        guard = self.guard(client)
        guard.embed(['synthetic'],'document')
        self.assertEqual(self.saves[0]['status'],'request_pending')
        self.assertEqual(self.saves[0]['pending'],{'role':'document','items':1})
    def test_cached_queries_cannot_call_unplanned_text_or_documents(self):
        client = FakeClient()
        cached = CachedQueries(client.config,['known'],[[1.0]])
        self.assertEqual(cached.embed(['known'],'query'),[[1.0]])
        with self.assertRaises(LibraryError): cached.embed(['unknown'],'query')
        with self.assertRaises(LibraryError): cached.embed(['known'],'document')
    def test_custom_request_cap_does_not_change_seed_default(self):
        client=FakeClient()
        guard=BudgetedEmbedder(client,{},lambda:None,max_requests=2,budget=Decimal('0.01'))
        for _ in range(2): guard.embed(['synthetic'],'document')
        with self.assertRaisesRegex(EmbeddingError,'request_limit'): guard.embed(['synthetic'],'document')
        self.assertEqual(MAX_REQUESTS,5)
    def test_invalid_custom_limits_rejected(self):
        for cap,budget in [(21,Decimal('0.15')),(True,Decimal('0.15')),(2,Decimal('NaN')),(2,Decimal('0.21'))]:
            with self.assertRaisesRegex(EmbeddingError,'invalid_batch_limits'):
                BudgetedEmbedder(FakeClient(),{},lambda:None,max_requests=cap,budget=budget)
    def test_review_scope_rejects_unplanned_text_before_call(self):
        client=FakeClient()
        guard=PlannedEmbedder(client,{},lambda:None,allowed_batches=[batch_hash(['reviewed'],'document')])
        with self.assertRaisesRegex(EmbeddingError,'outside_reviewed_scope'): guard.embed(['other'],'document')
        self.assertEqual(client.records,[])
    def test_review_scope_rejects_role_change_before_call(self):
        client=FakeClient()
        guard=PlannedEmbedder(client,{},lambda:None,allowed_batches=[batch_hash(['reviewed'],'document')])
        with self.assertRaisesRegex(EmbeddingError,'outside_reviewed_scope'): guard.embed(['reviewed'],'query')
        self.assertEqual(client.records,[])
    def test_review_scope_preserves_sequence_and_caps(self):
        client=FakeClient()
        allowed=[batch_hash(['first'],'document'),batch_hash(['second'],'query')]
        guard=PlannedEmbedder(client,{},lambda:None,allowed_batches=allowed)
        guard.embed(['first'],'document')
        guard.embed(['second'],'query')
        with self.assertRaisesRegex(EmbeddingError,'outside_reviewed_scope'): guard.embed(['third'],'query')
        self.assertEqual(len(client.records),2)


if __name__=='__main__': unittest.main()
