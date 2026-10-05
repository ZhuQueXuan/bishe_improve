import json
import sys
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import Mock

import build_medical_text as builder
from api.text_chunks import spans, chunks_for_record
from api.fulltext_retrieval import literal_query, valid_schema, retrieve_transaction, INDEX_STATUS, SEARCH
from api.graph_retrieval import RetrievalUnavailable, MARKER_QUERY


class TextTests(unittest.TestCase):
    def test_boundaries_restore_text(self):
        for text in ['', '糖尿病。' * 300, 'a' * 1600, '中文\n 空白\t。' * 100]:
            pieces = list(spans(text))
            self.assertEqual(''.join(p[2] for p in pieces), text)
            self.assertTrue(all(0 < len(p[2]) <= 600 and text[p[0]:p[1]] == p[2] for p in pieces))

    def test_list_preserves_index_and_original_spacing(self):
        record = {'name': '测试病', 'symptom': [' 咳嗽 ', None, '胸痛'], '_id': 1}
        proof = {'file': 'bishe/data/medical.json', 'file_sha256': 'a'*64, 'line': 1, 'record_id': 1}
        chunks = list(chunks_for_record(record, proof))
        self.assertEqual([c['text'] for c in chunks], [' 咳嗽 ', '胸痛'])
        self.assertEqual([c['source']['index'] for c in chunks], [0, 2])
        self.assertEqual(chunks, list(chunks_for_record(record, proof)))

    def test_whole_corpus_exact_source_and_reconstruction(self):
        raw = {line: json.loads(text) for line, text in enumerate(builder.SOURCE.read_text(encoding='utf-8').splitlines(), 1)}
        chunks = json.loads((builder.OUTPUT / 'chunks.json').read_text(encoding='utf-8'))
        grouped = defaultdict(list)
        seen = set()
        for chunk in chunks:
            self.assertNotIn(chunk['id'], seen)
            seen.add(chunk['id'])
            proof = chunk['source']
            record = raw[proof['line']]
            text = record[proof['field']]
            if 'index' in proof:
                text = text[proof['index']]
            self.assertEqual(record['_id'], proof['record_id'])
            self.assertEqual(record['name'].strip(), chunk['disease'])
            self.assertEqual(text[proof['start']:proof['end']], chunk['text'])
            self.assertEqual(chunk['medical_review_status'], 'not_verified')
            grouped[(proof['line'], proof['field'], proof.get('index'))].append(chunk)
        for (line, field, index), group in grouped.items():
            original = raw[line][field]
            if index is not None:
                original = original[index]
            group.sort(key=lambda c: c['source']['start'])
            self.assertEqual(''.join(c['text'] for c in group), original)
        self.assertEqual(len(chunks), 149975)

    def test_stable_regeneration(self):
        chunks, stats = builder.build()
        saved = json.loads((builder.OUTPUT / 'statistics.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['chunks_sha256'], builder.digest(builder.OUTPUT / 'chunks.json'))
        import hashlib
        self.assertEqual(hashlib.sha256(builder.encode(chunks)).hexdigest(), saved['chunks_sha256'])
        self.assertTrue(all(saved[k] == v for k, v in stats.items()))


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.index = {'name': 'medical_fulltext_v1', 'state': 'ONLINE',
                      'labelsOrTypes': ['CuratedEntity'], 'properties': ['name', 'properties_json'],
                      'options': {'indexConfig': {'fulltext.analyzer': 'cjk', 'fulltext.eventually_consistent': False}}}
        self.proof = {'file': 'bishe/data/medical.json', 'file_sha256': 'a'*64, 'line': 1,
                      'record_id': 1, 'disease': '测试病', 'field': 'desc', 'raw_value': '与呼吸道有关'}
        self.row = {'name': '测试病', 'properties_json': json.dumps({'desc': '与呼吸道有关'}),
                    'property_sources_json': json.dumps({'desc': self.proof}), 'score': 2.3}
        def run(query, **params):
            return {MARKER_QUERY: [{'state': 'complete', 'plan_sha256': 'a'*64}],
                    INDEX_STATUS: [self.index], SEARCH: [self.row]}[query]
        self.tx = Mock()
        self.tx.run.side_effect = run

    def test_lucene_operators_are_quoted(self):
        self.assertEqual(literal_query('a" OR *:* \\'), 'properties_json:"a\\" OR *:* \\\\"')

    def test_input_limits(self):
        for phrase in ['', ' ', None, 'a'*201]:
            with self.assertRaises(ValueError):
                literal_query(phrase)
        with self.assertRaises(ValueError):
            retrieve_transaction(self.tx, '呼吸道', True)

    def test_index_contract(self):
        self.assertTrue(valid_schema(self.index))
        self.index['properties'] = ['name']
        self.assertFalse(valid_schema(self.index))

    def test_offline_index_refused(self):
        self.index['state'] = 'POPULATING'
        with self.assertRaises(RetrievalUnavailable):
            retrieve_transaction(self.tx, '呼吸道')
        self.assertFalse(any(call.args[0] == SEARCH for call in self.tx.run.call_args_list))

    def test_source_conflict_refused(self):
        self.proof['raw_value'] = '伪造内容'
        self.row['property_sources_json'] = json.dumps({'desc': self.proof})
        with self.assertRaises(RetrievalUnavailable):
            retrieve_transaction(self.tx, '呼吸道')

    def test_exact_span_and_no_writes(self):
        result = retrieve_transaction(self.tx, '呼吸道')
        self.assertEqual(result['status'], 'found')
        self.assertEqual(result['facts'][0]['value'], '与呼吸道有关')
        self.assertEqual(result['facts'][0]['sources'][0]['start'], 0)
        self.assertTrue(all(call.args[0] in {MARKER_QUERY, INDEX_STATUS, SEARCH} for call in self.tx.run.call_args_list))

    def test_irrelevant_candidate_not_evidence(self):
        self.assertEqual(retrieve_transaction(self.tx, '不存在词')['status'], 'no_evidence')


if __name__ == '__main__':
    unittest.main()
