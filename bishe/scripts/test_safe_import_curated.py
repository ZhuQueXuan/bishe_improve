"""Offline boundary/transaction tests. Simulated DB only; no real credentials or sockets."""
import argparse
import ast
import copy
import importlib.util
import io
import json
import re
import socket
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import safe_import_curated as importer


class Result(list):
    def single(self):
        return self[0] if self else None

    def consume(self):
        return None


class FakeTransaction:
    """Models parameterized row transport and transaction state, not Neo4j Cypher syntax."""
    def __init__(self, driver):
        self.driver = driver

    def run(self, query, **params):
        d = self.driver
        d.calls.append((query, copy.deepcopy(params)))
        if query in (importer.NODE_CONSTRAINT, importer.MARKER_CONSTRAINT):
            d.schema.add(query)
            return Result()
        if query.startswith('SHOW CONSTRAINTS'):
            return Result([{'name': 'curated_entity_key', 'type': 'UNIQUENESS', 'labelsOrTypes': ['CuratedEntity'], 'properties': ['dataset_id', 'id']},
                           {'name': 'curated_dataset_key', 'type': 'UNIQUENESS', 'labelsOrTypes': ['CuratedDataset'], 'properties': ['dataset_id']}])
        if query.startswith('MERGE (m:CuratedDataset'):
            if not d.state['markers']:
                d.state['markers'] = [{'fingerprint': params['fingerprint']}]
            return Result(copy.deepcopy(d.state['markers']))
        if query.startswith('MATCH (m:CuratedDataset'):
            return Result(copy.deepcopy(d.state['markers']))
        if query.startswith('MATCH (n:CuratedEntity') and 'sum(CASE' not in query:
            return Result(copy.deepcopy(list(d.state['nodes'].values())))
        if query.startswith('MATCH (a)-[r]'):
            return Result(copy.deepcopy(list(d.state['edges'].values())))
        if query == importer.NODE_QUERY:
            for row in params['rows']:
                d.state['nodes'].setdefault(row['id'], copy.deepcopy(row))
            return Result([{'processed': len(params['rows'])}])
        if query.startswith('UNWIND $rows AS row') and 'MERGE (a)-[r:' in query:
            if d.fail_relations:
                raise RuntimeError('SIMULATED PRIVATE ERROR SHOULD NEVER BE PRINTED')
            relation_type = re.search(r'MERGE \(a\)-\[r:(\w+)', query).group(1)
            count = 0
            for row in params['rows']:
                if row['start_id'] in d.state['nodes'] and row['end_id'] in d.state['nodes']:
                    d.state['edges'].setdefault(row['id'], {**copy.deepcopy(row), 'type': relation_type,
                         'start_dataset': importer.DATASET, 'end_dataset': importer.DATASET})
                    count += 1
            return Result([{'processed': count}])
        if 'sum(CASE' in query:
            used = {r[k] for r in d.state['edges'].values() for k in ('start_id', 'end_id')}
            return Result([{'empty_names': sum(not n['name'].strip() for n in d.state['nodes'].values()),
                           'isolated_nodes': len(set(d.state['nodes']) - used),
                           'missing_sources': sum(not n['sources_json'] or n['sources_json'] == '[]' for n in d.state['nodes'].values())}])
        raise AssertionError('Unexpected query in offline simulation')


class FakeSession:
    def __init__(self, driver, options):
        self.driver, self.options = driver, options

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def run(self, query, **params):
        if self.options.get('default_access_mode') == 'READ':
            if not query.startswith('MATCH'):
                raise AssertionError('Write in read-only session')
        return FakeTransaction(self.driver).run(query, **params)

    def execute_read(self, callback, *args):
        before = copy.deepcopy(self.driver.state)
        result = callback(FakeTransaction(self.driver), *args)
        if self.driver.state != before:
            raise AssertionError('Read callback mutated state')
        return result

    def execute_write(self, callback, *args):
        self.driver.write_transactions += 1
        original = copy.deepcopy(self.driver.state)
        try:
            return callback(FakeTransaction(self.driver), *args)
        except Exception:
            self.driver.state = original
            raise


class FakeDriver:
    def __init__(self):
        self.state = {'markers': [], 'nodes': {}, 'edges': {}}
        self.schema, self.calls, self.sessions = set(), [], []
        self.write_transactions, self.fail_relations = 0, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def session(self, **options):
        self.sessions.append(options)
        return FakeSession(self, options)


class ImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = importer.validate_dataset(importer.builder.DEFAULT_OUTPUT)
        cls.plan = importer.build_plan(cls.package, uri='bolt://127.0.0.1:7687', database='neo4j')

    def args(self, apply=True):
        return argparse.Namespace(apply=apply, uri='bolt://127.0.0.1:7687', database='neo4j', confirm=None,
                                  review_file=None, backup_ref=None)

    def run_main(self, args):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = importer.main(args)
        return result, json.loads(buffer.getvalue())

    def test_default_and_dry_run_never_connect(self):
        with patch.object(importer, 'connect', side_effect=AssertionError('must not connect')):
            for args in ([], ['--dry-run']):
                code, report = self.run_main(args)
                self.assertEqual(code, 0)
                self.assertFalse(report['database_access'])
                self.assertEqual((report['entities'], report['relations'], report['diseases']), (1390, 2138, 40))
                self.assertIsNone(report['database_changes']['new_entities'])
                self.assertEqual(report['database_changes']['deletions'], 0)

    def test_unapproved_apply_is_rejected_before_connect(self):
        with patch.object(importer, 'connect', side_effect=AssertionError('must not connect')):
            code, report = self.run_main(['--apply'])
        self.assertEqual(code, 2)
        self.assertEqual(report['write_status'], 'not_started')

    def test_batch_plan_and_fingerprint_binding(self):
        self.assertEqual((len(self.plan['node_batches']), len(self.plan['relation_batches'])), (3, 11))
        variants = [importer.build_plan(self.package, uri='bolt://127.0.0.1:7687', database='other'),
                    importer.build_plan(self.package, batch_size=100),
                    importer.build_plan(self.package)]
        for variant in variants:
            self.assertNotEqual(self.plan['report']['plan_sha256'], variant['report']['plan_sha256'])
        self.assertEqual(self.plan, importer.build_plan(self.package, uri='bolt://127.0.0.1:7687', database='neo4j'))

    def test_schema_and_target_strings_are_allowlisted(self):
        with self.assertRaises(importer.ImportProblem):
            importer.relation_query('has_symptom] DELETE n //')
        for uri in ('https://localhost', 'bolt://user:password@localhost', 'bolt://localhost?password=x'):
            with self.assertRaises(importer.ImportProblem):
                importer.build_plan(self.package, uri=uri)
        with self.assertRaises(importer.ImportProblem):
            importer.build_plan(self.package, database='neo4j; DELETE n')
        for size in (0, -1, 1001):
            with self.assertRaises(importer.ImportProblem):
                importer.build_plan(self.package, batch_size=size)

    def test_read_only_target_preview_reports_matches_without_writes(self):
        driver = FakeDriver()
        before = copy.deepcopy(driver.state)
        report = importer.online_run(self.package, self.plan, self.args(False), lambda uri: driver)
        self.assertEqual(driver.state, before)
        self.assertFalse(driver.schema)
        self.assertEqual(driver.write_transactions, 0)
        self.assertEqual(report['database_changes']['new_entities'], 1390)
        self.assertEqual(report['database_changes']['new_relations'], 2138)
        self.assertEqual(driver.sessions[0]['default_access_mode'], 'READ')

    def test_apply_and_repeat_are_idempotent_in_transaction_simulation(self):
        driver = FakeDriver()
        with patch.object(importer, 'check_apply_permission', return_value={'backup_reference_sha256': 'simulated'}):
            first = importer.online_run(self.package, self.plan, self.args(), lambda uri: driver)
            original = copy.deepcopy(driver.state)
            second = importer.online_run(self.package, self.plan, self.args(), lambda uri: driver)
        self.assertEqual(driver.state, original)
        self.assertEqual(driver.write_transactions, 2)
        self.assertEqual(first['database_changes']['new_entities'], 1390)
        self.assertEqual(first['database_changes']['new_relations'], 2138)
        self.assertEqual(second['database_changes']['new_entities'], 0)
        self.assertEqual(second['database_changes']['new_relations'], 0)
        self.assertEqual(second['database_changes']['matched_entities'], 1390)
        self.assertEqual(second['post_commit_validation']['relations'], 2138)
        self.assertEqual(second['write_status'], 'committed_and_verified')

    def test_batch_failure_rolls_back_all_data(self):
        driver = FakeDriver()
        driver.fail_relations = True
        original = copy.deepcopy(driver.state)
        with patch.object(importer, 'check_apply_permission', return_value={}):
            with self.assertRaises(RuntimeError):
                importer.online_run(self.package, self.plan, self.args(), lambda uri: driver)
        self.assertEqual(driver.state, original)
        self.assertEqual(driver.write_transactions, 1)
        self.assertEqual(len(driver.schema), 2)  # Schema has a separate commit; report/docs must say so.

    def test_conflicting_existing_content_is_not_overwritten(self):
        driver = FakeDriver()
        importer.import_transaction(FakeTransaction(driver), self.package, self.plan, 'simulation')
        key = next(iter(driver.state['nodes']))
        # A dishonest payload hash cannot hide actual content corruption.
        driver.state['nodes'][key]['sources_json'] = '[]'
        before = copy.deepcopy(driver.state)
        with patch.object(importer, 'check_apply_permission', return_value={}):
            with self.assertRaises(importer.ImportProblem):
                importer.online_run(self.package, self.plan, self.args(), lambda uri: driver)
        self.assertEqual(driver.state, before)
        self.assertEqual(driver.write_transactions, 0)

    def test_duplicate_unknown_and_cross_namespace_records_fail(self):
        driver = FakeDriver()
        importer.import_transaction(FakeTransaction(driver), self.package, self.plan, 'simulation')
        target = importer.read_target(FakeTransaction(driver), importer.DATASET)
        target['nodes'].append(copy.deepcopy(target['nodes'][0]))
        target['edges'][0]['end_dataset'] = 'old_dataset'
        result = importer.compare_target(self.package, target)
        self.assertGreaterEqual(len(result['conflicts']), 2)
        with self.assertRaises(importer.ImportProblem):
            importer.validate_marker({'markers': [], 'nodes': [{}], 'edges': []}, 'fingerprint')

    def test_exact_plan_and_authorized_review_are_required(self):
        args = self.args()
        args.confirm = self.plan['report']['plan_sha256']
        args.review_file = Path('simulated-review.json')
        args.backup_ref = Path('simulated-backup-record.json')
        review = {'plan_sha256': args.confirm, 'engineering_demo_review': 'approved', 'database_write_authorized': True}
        with patch.object(Path, 'read_text', return_value=json.dumps(review)), patch.object(Path, 'is_file', return_value=True), \
                patch.object(Path, 'stat', return_value=argparse.Namespace(st_size=100)), patch.object(importer.builder, 'digest', return_value='simulated'):
            self.assertIn('backup_reference_sha256', importer.check_apply_permission(args, self.plan['report']))
            args.confirm = 'stale'
            with self.assertRaises(importer.ImportProblem):
                importer.check_apply_permission(args, self.plan['report'])
            args.confirm = self.plan['report']['plan_sha256']
            review['database_write_authorized'] = False
            with patch.object(Path, 'read_text', return_value=json.dumps(review)):
                with self.assertRaises(importer.ImportProblem):
                    importer.check_apply_permission(args, self.plan['report'])

    def test_data_is_always_parameterized_and_json_evidence_is_lossless(self):
        node = copy.deepcopy(self.package['nodes'][0])
        node['name'] = '测试引号"与符号$和反引号`'
        row = importer.node_row(node)
        self.assertNotIn(node['name'], importer.NODE_QUERY)
        self.assertEqual(json.loads(row['sources_json']), node['sources'])
        self.assertEqual(json.loads(row['properties_json']), node.get('properties', {}))
        for batch in self.plan['relation_batches']:
            self.assertNotIn(batch['rows'][0]['id'], importer.relation_query(batch['type']))

    def test_invalid_input_is_rejected_without_partial_skipping(self):
        source_dir = importer.builder.DEFAULT_OUTPUT
        real_read = Path.read_text
        def validate_mutation(mutator):
            docs = {name: json.loads(real_read(source_dir / name, encoding='utf-8')) for name in importer.CORE_FILES}
            mutator(docs)
            def fake_read(path, *args, **kwargs):
                return json.dumps(docs[path.name], ensure_ascii=False) if path.parent == source_dir and path.name in docs else real_read(path, *args, **kwargs)
            with patch.object(Path, 'read_text', fake_read):
                with self.assertRaises(importer.ImportProblem):
                    importer.validate_dataset(source_dir)
        validate_mutation(lambda docs: docs['relations.json'][0].update(end_id='missing'))
        validate_mutation(lambda docs: docs['entities.json'].append(docs['entities.json'][0]))
        validate_mutation(lambda docs: docs['entities.json'][0]['sources'][0].update(raw_value='fabricated'))
        validate_mutation(lambda docs: docs['entities.json'][0].update(medical_review_status='verified'))

    def test_report_cannot_overwrite_source_or_existing_files(self):
        for path in (importer.builder.SOURCE, importer.builder.WORKSPACE / 'temp/curated_v1_tests.log'):
            with self.assertRaises(importer.ImportProblem):
                importer.report_destination(path)
        with patch.object(Path, 'exists', return_value=True):
            with self.assertRaises(importer.ImportProblem):
                importer.report_destination(importer.builder.WORKSPACE / 'temp/existing.json')

    def test_wrong_named_constraint_blocks_data_transaction(self):
        driver = FakeDriver()
        real_run = FakeTransaction.run
        def wrong_constraint(tx, query, **params):
            result = real_run(tx, query, **params)
            if query.startswith('SHOW CONSTRAINTS'):
                result[0]['properties'] = ['name']
            return result
        with patch.object(importer, 'check_apply_permission', return_value={}), patch.object(FakeTransaction, 'run', wrong_constraint):
            with self.assertRaises(importer.ImportProblem):
                importer.online_run(self.package, self.plan, self.args(), lambda uri: driver)
        self.assertEqual(driver.write_transactions, 0)
        self.assertFalse(driver.state['nodes'])

    def test_supplied_backup_reference_must_exist(self):
        args = self.args()
        args.confirm = self.plan['report']['plan_sha256']
        args.review_file, args.backup_ref = Path('simulated-review.json'), Path('simulated-backup.json')
        review = {'plan_sha256': args.confirm, 'engineering_demo_review': 'approved', 'database_write_authorized': True}
        with patch.object(Path, 'read_text', return_value=json.dumps(review)), patch.object(Path, 'is_file', return_value=False):
            with self.assertRaises(importer.ImportProblem):
                importer.check_apply_permission(args, self.plan['report'])

    def test_errors_never_print_sensitive_driver_message(self):
        with patch.object(importer, 'validate_dataset', side_effect=RuntimeError('password=SECRET')):
            code, report = self.run_main([])
        self.assertEqual(code, 1)
        self.assertNotIn('SECRET', json.dumps(report))

    def test_legacy_entrypoints_are_absent_from_active_project(self):
        for relative in ('knowledge_graph_backend/neo4j_importer.py', 'knowledge_graph_backend/api/import_data.py'):
            self.assertFalse((importer.builder.PROJECT / relative).exists())

    def test_existing_user_backup_does_not_require_a_new_artifact(self):
        args = self.args()
        args.confirm = self.plan['report']['plan_sha256']
        args.review_file, args.backup_ref = Path('simulated-review.json'), None
        review = {'plan_sha256': args.confirm, 'engineering_demo_review': 'approved', 'database_write_authorized': True}
        with patch.object(Path, 'read_text', return_value=json.dumps(review)), patch.object(importer.builder, 'digest', return_value='review-hash'):
            result = importer.check_apply_permission(args, self.plan['report'])
        self.assertIsNone(result['backup_reference_sha256'])


if __name__ == '__main__':
    with patch.object(socket, 'create_connection', side_effect=AssertionError('Offline tests forbid network connections')), \
            patch.object(socket.socket, 'connect', side_effect=AssertionError('Offline tests forbid network connections')):
        unittest.main(verbosity=2)
