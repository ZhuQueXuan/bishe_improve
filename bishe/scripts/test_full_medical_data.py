"""Source fidelity and coverage tests for the full offline dataset."""
import json
import unittest
from collections import Counter

import build_full_medical_data as full


class FullDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = full.OUTPUT
        cls.nodes = json.loads((cls.directory / 'entities.json').read_text(encoding='utf-8'))
        cls.edges = json.loads((cls.directory / 'relations.json').read_text(encoding='utf-8'))
        cls.samples = json.loads((cls.directory / 'source_records.json').read_text(encoding='utf-8'))
        cls.excluded = json.loads((cls.directory / 'excluded_records.json').read_text(encoding='utf-8'))
        cls.stats = json.loads((cls.directory / 'statistics.json').read_text(encoding='utf-8'))
        cls.originals = dict(full.core.load_records(full.core.SOURCE)[0])
        cls.lookup = {node['id']: node for node in cls.nodes}

    def proof_matches(self, proof):
        record = self.originals[proof['line']]
        self.assertEqual(proof['file_sha256'], self.stats['source_file_sha256']['medical.json'])
        self.assertEqual(proof['record_id'], record['_id'])
        self.assertEqual(proof['disease'], record['name'])
        value = record[proof['field']]
        if 'index' in proof:
            value = value[proof['index']]
        self.assertEqual(proof['raw_value'], value)

    def test_every_original_record_is_preserved_or_explicitly_excluded(self):
        selected = {sample['source']['line'] for sample in self.samples}
        excluded = {record['line'] for record in self.excluded}
        self.assertFalse(selected & excluded)
        self.assertEqual(selected | excluded, set(self.originals))
        self.assertEqual(len(self.samples), 8806)
        for sample in self.samples:
            self.assertEqual(sample['raw_record'], self.originals[sample['source']['line']])
        for sample in self.excluded:
            self.assertEqual(sample['raw_record'], self.originals[sample['line']])

    def test_missing_optional_fields_do_not_exclude_diseases(self):
        self.assertGreater(sum(not sample['raw_record'].get('do_eat') for sample in self.samples), 3000)
        names = {node['name'] for node in self.nodes if node['type'] == 'Disease'}
        self.assertNotIn('无标题文档', names)
        self.assertIn('骨质疏松', names)
        self.assertEqual(sum(node['name'] == '胎膜早破' and node['type'] == 'Disease' for node in self.nodes), 1)

    def test_all_entities_properties_and_sources_match_original(self):
        self.assertEqual(len(self.lookup), len(self.nodes))
        self.assertEqual(len({(n['type'], n['name']) for n in self.nodes}), len(self.nodes))
        for node in self.nodes:
            self.assertIsNone(full.core.bad_name(node['name']))
            self.assertTrue(node['sources'])
            for proof in node['sources']:
                self.proof_matches(proof)
                self.assertEqual(node['name'], proof['raw_value'].strip())
            for field, proof in node.get('property_sources', {}).items():
                self.proof_matches(proof)
                self.assertEqual(node['properties'][field], proof['raw_value'])

    def test_every_relation_endpoint_type_key_and_source_is_valid(self):
        unique, used = set(), set()
        for edge in self.edges:
            start, end = self.lookup[edge['start_id']], self.lookup[edge['end_id']]
            triple = (edge['type'], edge['start_id'], edge['end_id'])
            self.assertNotIn(triple, unique)
            unique.add(triple)
            used.update((edge['start_id'], edge['end_id']))
            self.assertEqual(edge['id'], full.core.identifier(edge['type'], edge['start_id'] + '\0' + edge['end_id']))
            for proof in edge['sources']:
                self.proof_matches(proof)
                expected = ('Disease', 'acompany_with') if proof['field'] == 'acompany' else full.core.FIELDS[proof['field']]
                self.assertEqual((end['type'], edge['type']), expected)
                self.assertEqual(start['type'], 'Disease')
                self.assertEqual(start['name'], proof['disease'].strip())
                self.assertEqual(end['name'], proof['raw_value'].strip())
        self.assertEqual(used, set(self.lookup))

    def test_contradictory_food_is_omitted_without_removing_the_disease(self):
        do_eat, no_eat = set(), set()
        for edge in self.edges:
            if edge['type'] == 'do_eat':
                do_eat.add((edge['start_id'], edge['end_id']))
            elif edge['type'] == 'no_eat':
                no_eat.add((edge['start_id'], edge['end_id']))
        self.assertFalse(do_eat & no_eat)
        self.assertIn(full.core.identifier('Disease', '痛风'), self.lookup)
        self.assertNotIn((full.core.identifier('Disease', '痛风'), full.core.identifier('Food', '白扁豆')), do_eat | no_eat)

    def test_no_product_generation_or_false_clinical_approval(self):
        self.assertNotIn('Producer', {node['type'] for node in self.nodes})
        self.assertNotIn('drugs_of', {edge['type'] for edge in self.edges})
        for item in self.nodes + self.edges:
            self.assertEqual(item['medical_review_status'], 'not_verified')
            self.assertFalse(any(proof['field'] == 'drug_detail' for proof in item['sources']))

    def test_statistics_and_original_fingerprints_are_exact(self):
        self.assertEqual(self.stats['graph']['entities'], len(self.nodes))
        self.assertEqual(self.stats['graph']['relations'], len(self.edges))
        self.assertEqual(self.stats['graph']['entities_by_type'], dict(Counter(n['type'] for n in self.nodes)))
        self.assertEqual(self.stats['graph']['relations_by_type'], dict(Counter(e['type'] for e in self.edges)))
        for name, expected in self.stats['source_file_sha256'].items():
            self.assertEqual(full.core.source_fingerprints()[name], expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
