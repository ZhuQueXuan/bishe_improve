"""Offline lifecycle and cross-store tests. Fixtures live under project temp; no cleanup deletes."""
import hashlib
import io
import json
import socket
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from api.document_library import (DocumentLibrary, LibraryError, inspect_pdf, page_spans,
                                  terms, lexical_search, digest)
from api.document_vectors import DocumentVectors, LocalQdrant, hybrid_search

TEMP = Path(__file__).resolve().parents[3] / 'temp/documents/tests'
FINGERPRINT = digest(b'offline-synthetic-embedding-only-v1')


def fixture(text):
    writer = PdfWriter()
    page = writer.add_blank_page(600,800)
    font = DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),
                             NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    stream = DecodedStreamObject()
    stream.set_data(('BT /F1 12 Tf 40 700 Td ('+text+') Tj ET').encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class Config:
    def fingerprint(self):
        return FINGERPRINT


class Embedder:
    config = Config()
    def __init__(self, callback=None):
        self.calls = 0
        self.callback = callback
    def embed(self, texts, role):
        self.calls += 1
        if self.callback:
            self.callback()
        return [[1.0]+[0.0]*1023 for _ in texts]


class FakeQdrant:
    def __init__(self):
        self.collections = {}
        self.fail_upsert = False
        self.corrupt = False
    def request(self, method, path, body=None):
        name = path.split('/')[2]
        if method == 'GET':
            if name not in self.collections:
                raise LibraryError('qdrant_not_found')
            return {'config':{'params':{'vectors':{'size':1024,'distance':'Cosine'}}}}
        if method == 'PUT' and path.endswith(name):
            self.collections[name] = {}
            return True
        store = self.collections[name]
        if method == 'PUT':
            if self.fail_upsert:
                raise LibraryError('injected_upsert_failure')
            store.update({row['id']:json.loads(json.dumps(row)) for row in body['points']})
            return {'status':'completed'}
        if 'ids' in body:
            rows = [json.loads(json.dumps(store[key])) for key in body['ids'] if key in store]
            if self.corrupt and rows:
                rows[0]['payload']['text_sha256'] = 'bad'
            return rows
        versions = body['filter']['must'][0]['match']['any']
        fp = body['filter']['must'][1]['match']['value']
        return {'points':[dict(row,score=1.0) for row in store.values()
                          if row['payload']['version_id'] in versions and row['payload']['fingerprint']==fp][:body['limit']]}


class LibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network_block = patch.object(socket,'socket',side_effect=AssertionError('network forbidden'))
        cls.network_block.start()
    @classmethod
    def tearDownClass(cls):
        cls.network_block.stop()
    def setUp(self):
        self.root = TEMP / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.lib = DocumentLibrary(self.root/'library')
        self.path = self.root/'source.pdf'
        self.path.write_bytes(fixture('alpha monitoring blood pressure. '*25))
        self.meta = {'doc_id':'test-guideline','title':'Test guideline','edition':'1','publisher':'Test fixture',
                     'source_url':'https://example.org/guide','download_url':'https://example.org/guide.pdf',
                     'published_date':'2025-01-01','effective_date':None,'acquired_at':'2026-10-23T00:00:00Z',
                     'rights':'synthetic','scope_note':'Synthetic engineering fixture only.',
                     'selected_pages':[1],'printed_page_offset':0,'sha256':digest(self.path.read_bytes())}
    def imported(self):
        return self.lib.import_pdf(self.path,self.meta)['version_id']
    def store(self):
        return DocumentVectors(self.lib,FINGERPRINT,FakeQdrant())
    def new_version(self):
        self.meta['edition'] = '2'
        return self.imported()

    def test_preview_does_not_create_catalog(self):
        self.assertTrue(inspect_pdf(self.path,self.meta)['chunks'])
        self.assertFalse(self.lib.database.exists())
    def test_import_staged_and_restart_persists(self):
        version = self.imported()
        self.assertEqual(self.lib.active_ids(),[])
        self.assertEqual(DocumentLibrary(self.lib.root).versions()[0]['id'],version)
    def test_exact_duplicate_is_idempotent(self):
        a = self.imported()
        self.assertTrue(self.lib.import_pdf(self.path,self.meta)['duplicate'])
        self.assertEqual(len(self.lib.versions()),1)
        self.assertTrue(self.lib.verify(a))
    def test_acquisition_time_does_not_create_new_version(self):
        a = self.imported()
        self.meta['acquired_at'] = '2026-10-24T00:00:00Z'
        self.assertEqual(a,self.imported())
    def test_update_remains_staged_and_rollback_retains_old(self):
        a = self.imported()
        self.lib.activate(a)
        b = self.new_version()
        self.assertEqual(self.lib.active_ids(),[a])
        self.lib.activate(b)
        self.assertEqual(self.lib.active_ids(),[b])
        self.lib.activate(a)
        self.assertEqual(self.lib.active_ids(),[a])
        self.assertEqual(len(self.lib.versions()),2)
    def test_disable_retains_sources(self):
        a = self.imported()
        self.lib.activate(a)
        self.lib.disable(self.meta['doc_id'])
        self.assertEqual(self.lib.active_ids(),[])
        self.assertTrue(self.lib.verify(a))
    def test_bad_update_preserves_active(self):
        a = self.imported()
        self.lib.activate(a)
        self.path.write_bytes(b'bad')
        with self.assertRaises(LibraryError): self.imported()
        self.assertEqual(self.lib.active_ids(),[a])
        self.assertEqual(len(self.lib.versions()),1)
    def test_duplicate_across_document_ids_rejected(self):
        self.imported()
        self.meta['doc_id'] = 'different-document'
        with self.assertRaisesRegex(LibraryError,'another_document'): self.imported()
        self.assertEqual(len(self.lib.versions()),1)
    def test_stored_object_tampering_blocks_activation(self):
        a = self.imported()
        (self.lib.root/'objects'/(self.meta['sha256']+'.pdf')).write_bytes(b'changed')
        with self.assertRaisesRegex(LibraryError,'stored_object'): self.lib.activate(a)
        self.assertEqual(self.lib.active_ids(),[])
    def test_stored_text_tampering_blocks_search(self):
        a = self.imported()
        self.lib.activate(a)
        with self.lib.connection() as db: db.execute("UPDATE chunks SET text='injected'")
        with self.assertRaisesRegex(LibraryError,'chunks_mismatch'): lexical_search(self.lib,'alpha')
    def test_offsets_are_exact_and_printed_page_independent(self):
        self.meta['printed_page_offset'] = 10
        p = inspect_pdf(self.path,self.meta)
        for c in p['chunks']:
            self.assertEqual(c['text'],p['pages'][0][c['start']:c['end']])
            self.assertEqual(c['printed_page'],11)
    def test_overlap_covers_source_without_out_of_range_offsets(self):
        text = ('paragraph long enough.\n'*91)
        values = list(page_spans(text))
        covered = set()
        for s,e,t in values:
            self.assertEqual(text[s:e],t)
            self.assertLessEqual(len(t),600)
            covered.update(range(s,e))
        self.assertEqual(covered,set(range(len(text))))
        self.assertLess(values[1][0],values[0][1])
    def test_exclusion_marker_is_exact(self):
        self.path.write_bytes(fixture('intro paragraph. '*10 + 'TABLE_START table cells '*10))
        self.meta['sha256'] = digest(self.path.read_bytes())
        self.meta['page_text_end_before'] = {'1':'TABLE_START table cells TABLE_START'}
        with self.assertRaises(LibraryError): inspect_pdf(self.path,self.meta)
        self.meta['page_text_end_before'] = {'1':'TABLE_START'}
        with self.assertRaises(LibraryError): inspect_pdf(self.path,self.meta)
    def test_valid_table_tail_exclusion_keeps_original_page(self):
        self.path.write_bytes(fixture('intro paragraph. '*10+'TABLE_START cells'))
        self.meta['sha256'] = digest(self.path.read_bytes())
        self.meta['page_text_end_before'] = {'1':'TABLE_START'}
        prepared = inspect_pdf(self.path,self.meta)
        self.assertIn('TABLE_START',prepared['pages'][0])
        self.assertTrue(all('TABLE_START' not in c['text'] for c in prepared['chunks']))
    def test_cached_parsing_still_detects_catalog_changes(self):
        a = self.imported()
        self.lib.verify(a)
        with self.lib.connection() as db: db.execute("UPDATE versions SET pages='[\"bad\"]'")
        with self.assertRaisesRegex(LibraryError,'source_contract_mismatch'): self.lib.verify(a)
    def test_scanned_or_empty_page_rejected(self):
        self.path.write_bytes(fixture('short'))
        self.meta['sha256'] = digest(self.path.read_bytes())
        with self.assertRaisesRegex(LibraryError,'manual_extraction'): inspect_pdf(self.path,self.meta)
    def test_missing_hash_and_wrong_hash_rejected(self):
        self.meta['sha256'] = '0'*64
        with self.assertRaisesRegex(LibraryError,'hash_mismatch'): inspect_pdf(self.path,self.meta)
    def test_no_caller_can_set_medical_verified(self):
        self.meta['medical_review_status'] = 'verified'
        with self.assertRaisesRegex(LibraryError,'metadata_fields'): self.imported()
    def test_bool_page_number_rejected(self):
        self.meta['selected_pages'] = [True]
        with self.assertRaises(LibraryError): self.imported()
    def test_out_of_range_page_rejected(self):
        self.meta['selected_pages'] = [2]
        with self.assertRaises(LibraryError): self.imported()
    def test_url_with_credentials_rejected(self):
        self.meta['source_url'] = 'https://secret:token@example.org/guide'
        with self.assertRaises(LibraryError): self.imported()
    def test_chinese_pdf_soft_wraps_are_joined_for_matching_only(self):
        self.assertIn('血压',terms('高血\n压 监测'))
        self.assertNotIn('压药',terms('血压。药物'))
    def test_search_excludes_staged_and_disabled_versions(self):
        a = self.imported()
        self.lib.activate(a)
        b = self.new_version()
        self.assertEqual({r['version_id'] for r in lexical_search(self.lib,'monitoring')},{a})
        self.lib.activate(b)
        self.assertEqual({r['version_id'] for r in lexical_search(self.lib,'monitoring')},{b})
        self.lib.disable(self.meta['doc_id'])
        self.assertEqual(lexical_search(self.lib,'monitoring'),[])
    def test_citation_rejects_other_version(self):
        a = self.imported()
        key = self.lib.chunks(a)[0]['id']
        with self.assertRaisesRegex(LibraryError,'outside'): self.lib.evidence(key,[])
    def test_unknown_activation_and_disable_do_not_mutate(self):
        a = self.imported()
        self.lib.activate(a)
        with self.assertRaises(LibraryError): self.lib.activate('unknown')
        with self.assertRaises(LibraryError): self.lib.disable('unknown')
        self.assertEqual(self.lib.active_ids(),[a])
    def test_index_readback_and_skip_without_new_embedding(self):
        a = self.imported()
        store, model = self.store(), Embedder()
        store.index_version(a,model)
        calls = model.calls
        self.assertTrue(store.index_version(a,model)['skipped'])
        self.assertEqual(model.calls,calls)
    def test_partial_qdrant_failure_does_not_mark_indexed(self):
        a = self.imported()
        store = self.store()
        store.qdrant.fail_upsert = True
        with self.assertRaises(LibraryError): store.index_version(a,Embedder())
        self.assertFalse(self.lib.indexed(a,FINGERPRINT,store.collection))
    def test_bad_readback_does_not_mark_indexed(self):
        a = self.imported()
        store = self.store()
        store.qdrant.corrupt = True
        with self.assertRaisesRegex(LibraryError,'payload_mismatch'): store.index_version(a,Embedder())
        self.assertFalse(self.lib.indexed(a,FINGERPRINT,store.collection))
    def test_wrong_embedder_is_blocked_before_call(self):
        a = self.imported()
        store = DocumentVectors(self.lib,'1'*64,FakeQdrant())
        model = Embedder()
        with self.assertRaisesRegex(LibraryError,'fingerprint_mismatch'): store.index_version(a,model)
        self.assertEqual(model.calls,0)
    def test_invalid_vectors_do_not_write_points(self):
        a = self.imported()
        model,store = Embedder(),self.store()
        model.embed = lambda texts,role:[[0.0]*1024 for _ in texts]
        with self.assertRaisesRegex(LibraryError,'vector_contract'): store.index_version(a,model)
        self.assertEqual(store.qdrant.collections[store.collection],{})
    def test_hybrid_requires_all_active_ready_before_query_call(self):
        a = self.imported()
        self.lib.activate(a)
        model = Embedder()
        with self.assertRaisesRegex(LibraryError,'not_indexed'):
            hybrid_search(self.lib,self.store(),'monitoring',model)
        self.assertEqual(model.calls,0)
    def test_old_qdrant_points_are_filtered_by_active_version(self):
        a = self.imported()
        b = self.new_version()
        store, model = self.store(), Embedder()
        for v in (a,b): store.index_version(v,model)
        self.lib.activate(b)
        result = hybrid_search(self.lib,store,'monitoring',model)
        self.assertEqual({r['version_id'] for r in result},{b})
        self.assertTrue(all(r['medical_review_status']=='not_verified' for r in result))
    def test_concurrent_switch_rejects_stale_result(self):
        a = self.imported()
        b = self.new_version()
        store = self.store()
        for v in (a,b): store.index_version(v,Embedder())
        self.lib.activate(a)
        with self.assertRaisesRegex(LibraryError,'changed_during'):
            hybrid_search(self.lib,store,'monitoring',Embedder(lambda:self.lib.activate(b)))
    def test_local_transport_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(LibraryError,'opt_in'): LocalQdrant()


if __name__ == '__main__':
    unittest.main()
