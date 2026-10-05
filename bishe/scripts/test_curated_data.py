"""Offline data-contract and source-fidelity tests; never connects to services."""
import json
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import build_curated_data as builder


class CleaningTests(unittest.TestCase):
    def test_invalid_and_duplicate_labels_are_logged(self):
        values, issues = builder.clean_list({'symptom': [' 头痛 ', '头痛', '', None, '未知', '症状...', '症状…']}, 'symptom')
        self.assertEqual(values, [('头痛', 0, ' 头痛 ')])
        self.assertEqual(Counter(i['reason'] for i in issues),
                         Counter({'duplicate_value': 1, 'empty': 1, 'non_string': 1, 'placeholder': 1, 'truncated': 2}))

    def test_wrong_type_not_silently_split_or_coerced(self):
        values, issues = builder.clean_list({'check': '血常规'}, 'check')
        self.assertEqual(values, [])
        self.assertEqual(issues[0]['reason'], 'wrong_field_type')

    def test_parse_errors_keep_original_line_numbers(self):
        with patch.object(Path, 'read_text', return_value='{"name":"甲"}\n\ninvalid\n[]\n{"name":"乙"}\n'):
            records, errors, physical = builder.load_records(Path('unused.json'))
        self.assertEqual([line for line, _ in records], [1, 5])
        self.assertEqual(physical, 5)
        self.assertEqual(errors, [{'line': 2, 'reason': 'blank_line'}, {'line': 3, 'reason': 'invalid_json'},
                                  {'line': 4, 'reason': 'non_object_record'}])

    def complete_record(self):
        return {'name': '测试疾病', **{f: '有足够长度的原始描述文字仅用作验证数据完整度不代表医学内容' for f in builder.TEXT_FIELDS},
                **{f: [f + '条目'] for f in builder.FIELDS}}

    def test_conflicting_food_excludes_whole_disease(self):
        record = self.complete_record()
        record['do_eat'], record['not_eat'] = ['杏仁'], [' 杏仁 ']
        reasons, _, _ = builder.eligibility(record, set())
        self.assertIn('contradictory_food:杏仁', reasons)

    def test_duplicate_disease_requires_manual_resolution(self):
        reasons, _, _ = builder.eligibility(self.complete_record(), {'测试疾病'})
        self.assertIn('duplicate_disease_name_requires_manual_resolution', reasons)

    def test_empty_core_field_is_not_fabricated(self):
        record = self.complete_record()
        record['common_drug'] = []
        reasons, _, _ = builder.eligibility(record, set())
        self.assertIn('no_usable_values:common_drug', reasons)


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = builder.DEFAULT_OUTPUT
        cls.entities = json.loads((cls.directory / 'entities.json').read_text(encoding='utf-8'))
        cls.relations = json.loads((cls.directory / 'relations.json').read_text(encoding='utf-8'))
        cls.statistics = json.loads((cls.directory / 'statistics.json').read_text(encoding='utf-8'))
        cls.rows = dict(builder.load_records(builder.SOURCE)[0])
        cls.sample = json.loads((cls.directory / 'source_records.json').read_text(encoding='utf-8'))
        cls.lookup = {e['id']: e for e in cls.entities}

    def verify_source(self, source):
        raw = self.rows[source['line']]
        self.assertEqual(raw['_id'], source['record_id'])
        self.assertEqual(raw['name'], source['disease'])
        self.assertEqual(source['file_sha256'], self.statistics['source_file_sha256']['medical.json'])
        value = raw[source['field']]
        if 'index' in source:
            value = value[source['index']]
        self.assertEqual(value, source['raw_value'])

    def test_sample_count_scope_and_full_original_records(self):
        self.assertEqual(len(self.sample), 40)
        self.assertEqual(Counter(s['group'] for s in self.sample), Counter({g['name']: 5 for g in json.loads(builder.POLICY.read_text(encoding='utf-8'))['groups']}))
        for sample in self.sample:
            self.assertEqual(sample['raw_record'], self.rows[sample['source']['line']])
            reasons, _, _ = builder.eligibility(sample['raw_record'], {'胎膜早破'})
            self.assertEqual(reasons, [])

    def test_every_entity_and_property_matches_its_source(self):
        self.assertEqual(len(self.entities), len({(e['type'], e['name']) for e in self.entities}))
        for entity in self.entities:
            self.assertTrue(entity['name'])
            self.assertTrue(entity['sources'])
            for source in entity['sources']:
                self.verify_source(source)
                self.assertEqual(entity['name'], source['raw_value'].strip())
            for field, source in entity.get('property_sources', {}).items():
                self.verify_source(source)
                self.assertEqual(entity['properties'][field], source['raw_value'])

    def test_every_relation_has_real_endpoints_and_field_evidence(self):
        unique = set()
        for relation in self.relations:
            triple = (relation['type'], relation['start_id'], relation['end_id'])
            self.assertNotIn(triple, unique)
            unique.add(triple)
            start, end = self.lookup[relation['start_id']], self.lookup[relation['end_id']]
            self.assertEqual(start['type'], 'Disease')
            self.assertTrue(relation['sources'])
            for source in relation['sources']:
                self.verify_source(source)
                self.assertEqual(start['name'], source['disease'].strip())
                self.assertEqual(end['name'], source['raw_value'].strip())
                expected = ('Disease', 'acompany_with') if source['field'] == 'acompany' else builder.FIELDS[source['field']]
                self.assertEqual((end['type'], relation['type']), expected)

    def test_no_product_fabrication_or_unreviewed_claim_of_certification(self):
        self.assertEqual({e['type'] for e in self.entities}, {'Disease', 'Drug', 'Food', 'Check', 'Department', 'Symptom', 'Cure'})
        self.assertNotIn('drugs_of', {r['type'] for r in self.relations})
        for item in self.entities + self.relations:
            self.assertEqual(item['medical_review_status'], 'not_verified')
            self.assertFalse(any(s['field'] == 'drug_detail' for s in item['sources']))
        self.assertEqual(self.statistics['manual_review_status'], 'pending')

    def test_raw_source_and_recorded_legacy_fingerprints_unchanged(self):
        for name, expected in self.statistics['source_file_sha256'].items():
            self.assertEqual(builder.source_fingerprints()[name], expected)

    def test_repeat_generation_is_byte_identical(self):
        regenerated, _ = builder.make_package()
        for name, content in regenerated.items():
            self.assertEqual((self.directory / name).read_bytes(), content, name)

    def test_cli_refuses_existing_output_and_original_data_directory(self):
        command = [sys.executable, '-B', str(Path(builder.__file__))]
        for args, message in [([], 'refusing overwrite'),
                              (['--output', str(builder.SOURCE.parent)], 'Output must be')]:
            result = subprocess.run(command + args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn(message, result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
