"""Full medical dataset: offline default, bounded transactions, durable resume; no deletion."""
import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import safe_import_curated as core

DATASET = 'medical_full_v1'
DEFAULT_DATA = core.builder.PROJECT / 'data' / 'full_v1'


def build_plan(package, size=500, uri=None, database=None):
    core.require(package['stats']['dataset_version'] == 'full_v1', 'Full importer requires full_v1')
    core.require(type(size) is int and 1 <= size <= 1000, 'Batch size must be 1..1000')
    if uri:
        parsed = urlsplit(uri)
        core.require(parsed.scheme in {'bolt', 'bolt+s', 'bolt+ssc', 'neo4j', 'neo4j+s', 'neo4j+ssc'}
                     and parsed.hostname and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment, 'Invalid target URI')
    if database:
        import re
        core.require(re.fullmatch(r'[A-Za-z0-9_.-]+', database), 'Invalid database name')
    grouped = defaultdict(list)
    for edge in package['edges']:
        grouped[edge['type']].append(edge)
    # Keep references to input records, not additional copies of all serialized evidence.
    sequence = [('entity', None, rows) for rows in core.batches(package['nodes'], size)]
    sequence.extend(('relation', kind, rows) for kind in sorted(grouped)
                    for rows in core.batches(grouped[kind], size))
    binding = {'dataset_id': DATASET, 'input_file_sha256': package['file_hashes'],
               'batch_size': size, 'target_uri': uri, 'target_database': database,
               'storage_contract': 'CuratedEntity-v1-json-evidence',
               'transaction': 'bounded_batches_with_atomic_cursor_v1',
               'importer_sha256': core.builder.digest(Path(__file__)),
               'shared_validator_sha256': core.builder.digest(Path(core.__file__))}
    report = {**binding, 'plan_sha256': core.sha(binding), 'mode': 'offline_dry_run',
              'database_access': False, 'data_validation': 'passed',
              'entities': len(package['nodes']), 'relations': len(package['edges']),
              'diseases': package['stats']['diseases'], 'batches': len(sequence),
              'proposed_dataset_marker_nodes': 1,
              'proposed_relation_indexes': ['full_rel_' + kind for kind in sorted(core.REL_TYPES)], 'deletions': 0,
              'medical_review_status': 'not_verified', 'write_status': 'not_started'}
    return {'report': report, 'sequence': sequence}


def marker(tx):
    rows = list(tx.run('MATCH (m:CuratedDataset {dataset_id:$dataset}) RETURN properties(m) AS m', dataset=DATASET))
    core.require(len(rows) <= 1, 'Duplicate dataset markers')
    return dict(rows[0]['m']) if rows else None


def check_marker(value, plan):
    if value is not None:
        if value.get('state') == 'complete':
            core.require(value.get('fingerprint') == core.sha(plan['report']['input_file_sha256']),
                         'Completed target belongs to another dataset fingerprint')
        else:
            core.require(value.get('plan_sha256') == plan['report']['plan_sha256'], 'Target belongs to another import plan')
        cursor = value.get('completed_batches')
        core.require(type(cursor) is int and 0 <= cursor <= len(plan['sequence']), 'Invalid saved progress')
        core.require(value.get('state') in {'importing', 'complete'}, 'Invalid dataset state')
        core.require(value['state'] != 'complete' or cursor == len(plan['sequence']), 'Premature completion marker')


NODE_READ = ('MATCH (n:CuratedEntity {dataset_id:$dataset}) RETURN n.id AS id, '
             'n.name AS name, n.entity_type AS entity_type, n.sources_json AS sources_json, '
             'n.properties_json AS properties_json, n.property_sources_json AS property_sources_json, '
             'n.medical_review_status AS medical_review_status, n.payload_sha256 AS payload_sha256')
EDGE_READ = ('MATCH (a)-[r]->(b) WHERE r.dataset_id=$dataset RETURN r.id AS id, type(r) AS type, '
             'a.id AS start_id, b.id AS end_id, a.dataset_id AS start_dataset, b.dataset_id AS end_dataset, '
             'r.sources_json AS sources_json, r.medical_review_status AS medical_review_status, '
             'r.payload_sha256 AS payload_sha256')


def inspect(tx, package, plan):
    """Stream exact payload comparisons and require the database to equal the committed prefix."""
    saved = marker(tx)
    check_marker(saved, plan)
    cursor = saved['completed_batches'] if saved else 0
    expected_nodes, expected_edges = {}, {}
    for kind, _, rows in plan['sequence'][:cursor]:
        target = expected_nodes if kind == 'entity' else expected_edges
        target.update((row['id'], row) for row in rows)
    seen_nodes, seen_edges = set(), set()
    for row in tx.run(NODE_READ, dataset=DATASET):
        key = row['id']
        core.require(key in expected_nodes and key not in seen_nodes, 'Entity outside committed prefix or duplicate')
        expected = core.node_row(expected_nodes[key])
        core.require(all(row[field] == value for field, value in expected.items()), 'Entity content/source conflict')
        seen_nodes.add(key)
    for row in tx.run(EDGE_READ, dataset=DATASET):
        key = row['id']
        core.require(key in expected_edges and key not in seen_edges, 'Relation outside committed prefix or duplicate')
        expected = expected_edges[key]
        core.require(all(row[field] == value for field, value in core.edge_row(expected).items())
                     and row['type'] == expected['type'] and row['start_dataset'] == DATASET
                     and row['end_dataset'] == DATASET, 'Relation content/source/endpoint conflict')
        seen_edges.add(key)
    core.require(len(seen_nodes) == len(expected_nodes) and len(seen_edges) == len(expected_edges), 'Committed data is missing')
    return {'completed_batches': cursor, 'state': saved['state'] if saved else 'absent',
            'matched_entities': len(seen_nodes), 'matched_relations': len(seen_edges),
            'new_entities': len(package['nodes']) - len(seen_nodes),
            'new_relations': len(package['edges']) - len(seen_edges),
            'identity_sources_and_payload': 'passed', 'conflicts': 0, 'deletions': 0}


def initialize(tx, plan):
    row = tx.run('''MERGE (m:CuratedDataset {dataset_id:$dataset})
ON CREATE SET m.plan_sha256=$plan, m.fingerprint=$fingerprint,
 m.completed_batches=0, m.state='importing'
SET m.lock_token=$plan
RETURN properties(m) AS m''', dataset=DATASET, plan=plan['report']['plan_sha256'],
                 fingerprint=core.sha(plan['report']['input_file_sha256'])).single()
    check_marker(dict(row['m']), plan)


def commit_batch(tx, plan, index, before_commit=None):
    # Lock, data write, and cursor advance are one transaction. An exception rolls back all three.
    tx.run('MATCH (m:CuratedDataset {dataset_id:$dataset}) SET m.lock_token=$plan',
           dataset=DATASET, plan=plan['report']['plan_sha256']).consume()
    saved = marker(tx)
    check_marker(saved, plan)
    core.require(saved is not None and saved['state'] == 'importing'
                 and saved['completed_batches'] == index, 'Concurrent import or progress conflict; inspect before retry')
    kind, relation_type, raw_rows = plan['sequence'][index]
    rows = [core.node_row(row) if kind == 'entity' else core.edge_row(row) for row in raw_rows]
    # Refuse unexpected records rather than silently MERGE matching an altered entity/edge.
    if kind == 'entity':
        existing = tx.run('MATCH (n:CuratedEntity {dataset_id:$dataset}) WHERE n.id IN $ids RETURN count(n) AS c',
                          dataset=DATASET, ids=[r['id'] for r in rows]).single()['c']
    else:
        existing = tx.run('MATCH ()-[r:' + relation_type + ']->() WHERE r.dataset_id=$dataset AND r.id IN $ids RETURN count(r) AS c',
                          dataset=DATASET, ids=[r['id'] for r in rows]).single()['c']
    core.require(existing == 0, 'Uncommitted batch already has records; refusing overwrite')
    result = tx.run(core.NODE_QUERY if kind == 'entity' else core.relation_query(relation_type),
                    rows=rows, dataset_id=DATASET).single()
    core.require(result and result['processed'] == len(rows), 'Batch count or endpoints mismatch')
    if before_commit:
        before_commit(index)
    tx.run('MATCH (m:CuratedDataset {dataset_id:$dataset}) SET m.completed_batches=$cursor',
           dataset=DATASET, cursor=index + 1).consume()
    return len(rows)


def finish(tx, package, plan):
    tx.run('MATCH (m:CuratedDataset {dataset_id:$dataset}) SET m.lock_token=$plan',
           dataset=DATASET, plan=plan['report']['plan_sha256']).consume()
    result = inspect(tx, package, plan)
    core.require(result['completed_batches'] == len(plan['sequence']), 'Incomplete dataset')
    row = tx.run('''MATCH (n:CuratedEntity {dataset_id:$dataset})
RETURN sum(CASE WHEN n.name IS NULL OR trim(n.name)='' THEN 1 ELSE 0 END) AS empty_names,
 sum(CASE WHEN n.sources_json IS NULL OR n.sources_json='[]' THEN 1 ELSE 0 END) AS missing_sources,
 sum(CASE WHEN NOT EXISTS { MATCH (n)-[]-(:CuratedEntity {dataset_id:$dataset}) } THEN 1 ELSE 0 END) AS isolated_nodes''',
                 dataset=DATASET).single()
    core.require(row and all(value == 0 for value in row.values()), 'Final graph checks failed')
    tx.run('MATCH (m:CuratedDataset {dataset_id:$dataset}) SET m.state="complete"', dataset=DATASET).consume()
    return {**result, **dict(row), 'state': 'complete'}


def online_run(package, plan, args, driver_factory=core.connect, before_commit=None):
    core.require(args.uri and args.database, 'Explicit target required')
    approval = core.check_apply_permission(args, plan['report']) if args.apply else {}
    with driver_factory(args.uri) as driver:
        with driver.session(database=args.database, default_access_mode='READ', fetch_size=100) as session:
            before = session.execute_read(inspect, package, plan)
        result = {**plan['report'], 'database_access': True, 'preflight': before,
                  'mode': 'apply' if args.apply else 'read_only_target_preview'}
        if not args.apply:
            return result
        with driver.session(database=args.database, fetch_size=100) as session:
            session.run(core.NODE_CONSTRAINT).consume()
            session.run(core.MARKER_CONSTRAINT).consume()
            constraints = list(session.run('SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties '
                               'WHERE name IN $names RETURN name,type,labelsOrTypes,properties',
                               names=['curated_entity_key', 'curated_dataset_key']))
            expected = {'curated_entity_key': (['CuratedEntity'], ['dataset_id', 'id']),
                        'curated_dataset_key': (['CuratedDataset'], ['dataset_id'])}
            core.require(len(constraints) == 2 and {r['name'] for r in constraints} == set(expected)
                         and all(r['type'] == 'UNIQUENESS' and (r['labelsOrTypes'], r['properties']) == expected[r['name']]
                                 for r in constraints), 'Constraint definitions mismatch')
            for kind in sorted(core.REL_TYPES):
                session.run('CREATE INDEX full_rel_' + kind + ' IF NOT EXISTS FOR ()-[r:' + kind
                            + ']-() ON (r.dataset_id, r.id)').consume()
            session.run('CALL db.awaitIndexes(120)').consume()
            session.execute_write(initialize, plan)
            # Revalidate under the marker's write lock before consuming a saved cursor.
            def locked_inspect(tx):
                tx.run('MATCH (m:CuratedDataset {dataset_id:$dataset}) SET m.lock_token=$plan',
                       dataset=DATASET, plan=plan['report']['plan_sha256']).consume()
                return inspect(tx, package, plan)
            current = session.execute_write(locked_inspect)
            for index in range(current['completed_batches'], len(plan['sequence'])):
                session.execute_write(commit_batch, plan, index, before_commit)
                if (index + 1) % 25 == 0:
                    print('Committed batches: %d/%d' % (index + 1, len(plan['sequence'])), flush=True)
            checked = session.execute_write(finish, package, plan)
            durable = session.execute_read(inspect, package, plan)
            core.require(durable['state'] == 'complete', 'Completion not durable')
        return {**result, **approval, 'post_commit_validation': {**checked, **durable},
                'database_changes': {'new_entities': current['new_entities'], 'new_relations': current['new_relations'],
                                     'matched_entities': current['matched_entities'], 'matched_relations': current['matched_relations'],
                                     'conflicts': 0, 'deletions': 0}, 'write_status': 'committed_and_verified'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=DEFAULT_DATA)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--inspect-target', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--uri')
    parser.add_argument('--database')
    parser.add_argument('--batch-size', type=int, default=500)
    parser.add_argument('--confirm')
    parser.add_argument('--review-file', type=Path)
    parser.add_argument('--backup-ref', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    started = time.monotonic()
    destination = core.report_destination(args.report) if args.report else None
    result = {'status': 'failed', 'deletions': 0, 'write_status': 'not_started'}
    code = 0
    try:
        package = core.validate_dataset(args.data_dir, allow_full=True)
        plan = build_plan(package, args.batch_size, args.uri, args.database)
        result = online_run(package, plan, args) if args.apply or args.inspect_target else plan['report']
        result['status'] = 'passed'
    except Exception as error:
        result['error'] = str(error) if isinstance(error, core.ImportProblem) else type(error).__name__
        result['write_status'] = 'inspect_saved_progress_before_retry' if args.apply else 'not_started'
        code = 2
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if destination:
        with destination.open('x', encoding='utf-8') as stream:
            stream.write(encoded)
    print(encoded)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
