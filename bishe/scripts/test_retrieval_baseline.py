"""Offline checks for benchmark scoring: relevant evidence, source fidelity, and AND filters."""
import copy
import json
import unittest

import evaluate_retrieval_baseline as baseline


class BaselineTests(unittest.TestCase):
    def test_symptom_conditions_require_every_exact_source_item(self):
        records = {name: {'raw_record': {'name': name, 'symptom': values}}
                   for name, values in {'甲病': ['多尿', '多饮'], '乙病': ['多尿'], '丙病': ['烦渴多饮']}.items()}
        result = baseline.expected_for({'id': 'and', 'oracle': {'symptoms_all': ['多尿', '多饮']},
                                        'fields': ['has_symptom']}, records)
        self.assertEqual(result['diseases'], ['甲病'])
        self.assertEqual(len(result['evidence']), 2)

    def test_unbacked_expectation_is_an_evaluation_error(self):
        with self.assertRaises(ValueError):
            baseline.expected_for({'id': 'empty', 'diseases': ['甲病'], 'fields': ['need_check']},
                                  {'甲病': {'raw_record': {'name': '甲病', 'check': []}}})

    def test_fields_from_another_disease_do_not_cover_requested_disease(self):
        expected = {'status': 'found', 'diseases': ['甲病', '乙病'], 'fields': ['has_symptom'],
                    'evidence': {('甲病', 'has_symptom', '多尿'), ('乙病', 'has_symptom', '多饮')}}
        actual = {'status': 'found', 'diseases': ['甲病', '乙病'],
                  'facts': [{'disease': '甲病', 'field': 'has_symptom', 'value': '多尿'}]}
        result = baseline.score_case({}, actual, expected, [])
        self.assertIn('missing_disease_field', result['failures'])
        self.assertEqual(result['evidence_recall'], 0.5)

    def test_wrong_fields_fail_even_when_requested_evidence_is_present(self):
        expected = {'status': 'found', 'diseases': ['甲病'], 'fields': ['has_symptom'],
                    'evidence': {('甲病', 'has_symptom', '多尿')}}
        actual = {'status': 'found', 'diseases': ['甲病'], 'facts': [
            {'disease': '甲病', 'field': 'has_symptom', 'value': '多尿'},
            {'disease': '甲病', 'field': 'need_check', 'value': '血糖'}]}
        result = baseline.score_case({}, actual, expected, [])
        self.assertIn('field_set_mismatch', result['failures'])
        self.assertEqual(result['evidence_precision'], 0.5)

    def test_full_list_failure_independent_of_retrieval_truncation_flag(self):
        evidence = {('甲病', 'has_symptom', str(i)) for i in range(10)}
        expected = {'status': 'found', 'diseases': ['甲病'], 'fields': ['has_symptom'], 'evidence': evidence}
        actual = {'status': 'found', 'diseases': ['甲病'], 'truncated': False,
                  'facts': [dict(disease='甲病', field='has_symptom', value=str(i)) for i in range(8)]}
        result = baseline.score_case({'complete_evidence': True}, actual, expected, [])
        self.assertIn('incomplete_evidence', result['failures'])
        self.assertEqual(result['missing_evidence_count'], 2)

    def test_valid_source_and_content_required(self):
        raw = {1: {'name': '甲病', '_id': 1, 'symptom': ['多尿']}}
        proof = {'file': 'bishe/data/medical.json', 'file_sha256': 'hash', 'line': 1,
                 'record_id': 1, 'disease': '甲病', 'field': 'symptom', 'index': 0}
        result = {'facts': [{'disease': '甲病', 'field': 'has_symptom', 'value': '多尿',
                            'sources': [proof], 'medical_review_status': 'not_verified'}]}
        self.assertEqual(baseline.check_sources(result, raw, 'hash'), [])
        for key, value in [('file_sha256', 'wrong'), ('index', 4), ('record_id', 2), ('field', 'check')]:
            changed = copy.deepcopy(result)
            changed['facts'][0]['sources'][0][key] = value
            self.assertIn('source_content_mismatch', baseline.check_sources(changed, raw, 'hash'))

    def test_rejected_query_must_not_return_facts(self):
        result = baseline.score_case({}, {'status': 'no_evidence', 'facts': [
            dict(disease='甲病', field='has_symptom', value='多尿')]},
            {'status': 'no_evidence', 'diseases': [], 'fields': [], 'evidence': set()}, [])
        self.assertIn('unexpected_evidence', result['failures'])

    def test_original_eighteen_cases_are_preserved(self):
        old = json.loads((baseline.PROJECT / 'data/retrieval_cases_v1.json').read_text(encoding='utf-8'))
        new = json.loads((baseline.PROJECT / 'data/retrieval_baseline_v2.json').read_text(encoding='utf-8'))
        converted = []
        for item in new['cases'][:18]:
            case = dict(item)
            self.assertEqual(case.pop('category'), 'existing')
            if 'diseases' in case:
                case['disease'] = case.pop('diseases')[0]
            converted.append(case)
        self.assertEqual(old, converted)
        self.assertEqual(len(new['cases']), 50)
        self.assertEqual(len({case['id'] for case in new['cases']}), 50)


if __name__ == '__main__':
    unittest.main()
