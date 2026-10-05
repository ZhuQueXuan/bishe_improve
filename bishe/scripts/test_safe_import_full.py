"""Lightweight full-import protocol tests; no database connection."""
import copy
import unittest
from unittest.mock import patch

import safe_import_full as full


class FullProtocolTests(unittest.TestCase):
    def setUp(self):
        self.package = {'nodes': [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}],
                        'edges': [{'id': 'e', 'type': 'has_symptom'}],
                        'stats': {'dataset_version': 'full_v1', 'diseases': 1},
                        'file_hashes': {'entities.json': 'test'}}
        self.plan = full.build_plan(self.package, 2, 'bolt://127.0.0.1:18687', 'neo4j')

    def test_bounded_batches_preserve_every_record(self):
        sequence = self.plan['sequence']
        self.assertEqual([len(rows) for _, _, rows in sequence], [2, 1, 1])
        self.assertEqual([r['id'] for _, _, rows in sequence for r in rows], ['a', 'b', 'c', 'e'])

    def test_target_input_and_batch_size_change_plan(self):
        original = self.plan['report']['plan_sha256']
        for size, uri in [(1, 'bolt://127.0.0.1:18687'), (2, 'bolt://127.0.0.1:7687')]:
            self.assertNotEqual(original, full.build_plan(self.package, size, uri, 'neo4j')['report']['plan_sha256'])
        changed = copy.deepcopy(self.package)
        changed['file_hashes']['entities.json'] = 'changed'
        self.assertNotEqual(original, full.build_plan(changed, 2, 'bolt://127.0.0.1:18687', 'neo4j')['report']['plan_sha256'])

    def test_invalid_or_premature_completion_refused(self):
        for cursor, state, digest in [(1, 'complete', self.plan['report']['plan_sha256']),
                                       (-1, 'importing', self.plan['report']['plan_sha256']),
                                       (0, 'importing', 'another')]:
            with self.assertRaises(full.core.ImportProblem):
                full.check_marker({'completed_batches': cursor, 'state': state, 'plan_sha256': digest}, self.plan)

    def test_complete_dataset_survives_code_cleanup_but_not_changed_inputs(self):
        saved = {'state': 'complete', 'completed_batches': len(self.plan['sequence']),
                 'plan_sha256': 'previous-implementation',
                 'fingerprint': full.core.sha(self.package['file_hashes'])}
        full.check_marker(saved, self.plan)
        saved['fingerprint'] = 'different-input'
        with self.assertRaises(full.core.ImportProblem):
            full.check_marker(saved, self.plan)

    def test_offline_does_not_connect_or_prepare_all_evidence(self):
        with patch.object(full.core, 'connect', side_effect=AssertionError('Must remain offline')), \
             patch.object(full.core, 'node_row', side_effect=AssertionError('No eager serialization')):
            self.assertFalse(full.build_plan(self.package)['report']['database_access'])

    def test_curated_format_and_embedded_credentials_refused(self):
        changed = copy.deepcopy(self.package)
        changed['stats']['dataset_version'] = 'curated_v1'
        with self.assertRaises(full.core.ImportProblem):
            full.build_plan(changed)
        with self.assertRaises(full.core.ImportProblem):
            full.build_plan(self.package, uri='bolt://user:password@localhost:7687')

    def test_resume_refuses_missing_or_modified_committed_records(self):
        saved = {'state': 'importing', 'completed_batches': 1,
                 'plan_sha256': self.plan['report']['plan_sha256']}
        class Transaction:
            def __init__(self, rows):
                self.rows = rows
            def run(self, query, **params):
                return self.rows if query == full.NODE_READ else []
        with patch.object(full, 'marker', return_value=saved), \
             patch.object(full.core, 'node_row', side_effect=lambda row: {'id': row['id'], 'name': row['id']}):
            for rows in [[], [{'id': 'a', 'name': 'changed'}, {'id': 'b', 'name': 'b'}],
                         [{'id': 'a', 'name': 'a'}, {'id': 'a', 'name': 'a'}]]:
                with self.assertRaises(full.core.ImportProblem):
                    full.inspect(Transaction(rows), self.package, self.plan)
            result = full.inspect(Transaction([{'id': 'a', 'name': 'a'}, {'id': 'b', 'name': 'b'}]), self.package, self.plan)
            self.assertEqual(result['matched_entities'], 2)
            self.assertEqual(result['new_entities'], 1)

    def test_records_without_marker_are_not_adopted(self):
        class Transaction:
            def run(self, query, **params):
                return [{'id': 'a'}] if query == full.NODE_READ else []
        with patch.object(full, 'marker', return_value=None), self.assertRaises(full.core.ImportProblem):
            full.inspect(Transaction(), self.package, self.plan)


if __name__ == '__main__':
    unittest.main()
