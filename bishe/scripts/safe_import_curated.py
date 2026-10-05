"""Curated graph import: offline preview by default; no deletion operation exists."""
import argparse
import hashlib
import json
import re
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import build_curated_data as builder

DATASET = 'medical_demo_curated_v1'
ENTITY_LABEL = 'CuratedEntity'
MARKER_LABEL = 'CuratedDataset'
REL_TYPES = {rel: kind for kind, rel in builder.FIELDS.values()}
REL_TYPES['acompany_with'] = 'Disease'
CORE_FILES = ('entities.json', 'relations.json', 'source_records.json', 'statistics.json')
NODE_CONSTRAINT = ('CREATE CONSTRAINT curated_entity_key IF NOT EXISTS FOR (n:CuratedEntity) '
                   'REQUIRE (n.dataset_id, n.id) IS UNIQUE')
MARKER_CONSTRAINT = ('CREATE CONSTRAINT curated_dataset_key IF NOT EXISTS FOR (m:CuratedDataset) '
                     'REQUIRE m.dataset_id IS UNIQUE')
NODE_QUERY = '''UNWIND $rows AS row
MERGE (n:CuratedEntity {dataset_id: $dataset_id, id: row.id})
ON CREATE SET n.name=row.name, n.entity_type=row.entity_type,
 n.sources_json=row.sources_json, n.properties_json=row.properties_json,
 n.property_sources_json=row.property_sources_json,
 n.medical_review_status=row.medical_review_status, n.payload_sha256=row.payload_sha256
RETURN count(n) AS processed'''


class ImportProblem(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def require(condition, message):
    if not condition:
        raise ImportProblem(message)


def batches(rows, size):
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def validate_dataset(directory, allow_full=False):
    """Reject the entire package on error; never silently drop an input record."""
    directory = Path(directory).resolve()
    files = {}
    try:
        for name in CORE_FILES:
            files[name] = json.loads((directory / name).read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ImportProblem('Dataset file missing or invalid JSON') from error
    nodes, edges, samples, stats = (files[n] for n in CORE_FILES)
    require(isinstance(nodes, list) and isinstance(edges, list) and isinstance(samples, list), 'Expected list-based curated format')
    require(isinstance(stats, dict) and stats.get('dataset_version') in ({'curated_v1', 'full_v1'} if allow_full else {'curated_v1'}), 'Unknown dataset version')
    full = stats['dataset_version'] == 'full_v1'
    require(nodes and edges and samples, 'Empty dataset')
    original_hash = builder.digest(builder.SOURCE)
    require(stats.get('source_file_sha256', {}).get('medical.json') == original_hash, 'Original medical.json fingerprint differs')
    originals = dict(builder.load_records(builder.SOURCE)[0])
    selected = {}
    for sample in samples:
        require(isinstance(sample, dict) and isinstance(sample.get('source'), dict), 'Malformed source record')
        line = sample['source'].get('line')
        require(type(line) is int and line in originals, 'Invalid source line')
        require(sample.get('raw_record') == originals[line], 'Source snapshot differs from original')
        require(line not in selected, 'Repeated source record')
        selected[line] = originals[line]

    def check_proof(proof):
        require(isinstance(proof, dict), 'Malformed evidence')
        line = proof.get('line')
        require(type(line) is int and line in selected, 'Evidence outside selected records')
        raw = selected[line]
        require(proof.get('file') == 'bishe/data/medical.json' and proof.get('file_sha256') == original_hash,
                'Evidence file or fingerprint mismatch')
        require(proof.get('record_id') == raw['_id'] and proof.get('disease') == raw['name'], 'Evidence identity mismatch')
        field = proof.get('field')
        require(field in raw, 'Evidence field missing')
        value = raw[field]
        if 'index' in proof:
            index = proof['index']
            require(isinstance(value, list) and type(index) is int and 0 <= index < len(value), 'Invalid evidence index')
            value = value[index]
        require(proof.get('raw_value') == value, 'Evidence raw value mismatch')
        return field, value, raw['name'].strip()

    for sample in samples:
        field, value, disease = check_proof(sample['source'])
        require(field == 'name' and value.strip() == disease, 'Sample identity proof mismatch')

    lookup, seen_names = {}, set()
    for node in nodes:
        require(isinstance(node, dict), 'Malformed entity')
        kind, name = node.get('type'), node.get('name')
        require(kind in {'Disease', *REL_TYPES.values()}, 'Unknown or forbidden entity type')
        require(builder.bad_name(name) is None and name == name.strip(), 'Invalid entity name')
        require(node.get('id') == builder.identifier(kind, name), 'Unstable entity key')
        require(node['id'] not in lookup and (kind, name) not in seen_names, 'Duplicate entity')
        require(node.get('medical_review_status') == 'not_verified', 'Unapproved medical certification')
        require(isinstance(node.get('sources'), list) and node['sources'], 'Missing entity evidence')
        for proof in node['sources']:
            field, value, _ = check_proof(proof)
            expected_kind = 'Disease' if field == 'name' else builder.FIELDS.get(field, (None, None))[0]
            require(expected_kind == kind and isinstance(value, str) and value.strip() == name, 'Entity source mapping mismatch')
        properties = node.get('properties', {})
        require(isinstance(properties, dict), 'Entity properties must be a map')
        if kind == 'Disease':
            require(set(properties) <= set(builder.TEXT_FIELDS) if full else set(properties) == set(builder.TEXT_FIELDS), 'Unexpected disease properties')
            require(set(node.get('property_sources', {})) == set(properties), 'Missing property evidence')
            for field, value in properties.items():
                source_field, source_value, disease = check_proof(node['property_sources'][field])
                require(source_field == field and source_value == value and disease == name, 'Property evidence mismatch')
            require(all(source['disease'].strip() == name for source in node['sources']), 'Disease source identity mismatch')
        else:
            require(not properties and not node.get('property_sources'), 'Unexpected auxiliary properties')
        lookup[node['id']] = node
        seen_names.add((kind, name))
    require({n['name'] for n in nodes if n['type'] == 'Disease'} == {r['name'].strip() for r in selected.values()}, 'Disease sample mismatch')
    triples, edge_ids, used = set(), set(), set()
    outgoing = defaultdict(list)
    for edge in edges:
        require(isinstance(edge, dict), 'Malformed relation')
        relation_type = edge.get('type')
        require(relation_type in REL_TYPES, 'Unknown or forbidden relation type')
        start, end = edge.get('start_id'), edge.get('end_id')
        require(start in lookup and end in lookup, 'Missing relation endpoint')
        require(lookup[start]['type'] == 'Disease' and lookup[end]['type'] == REL_TYPES[relation_type], 'Endpoint type mismatch')
        require(edge.get('id') == builder.identifier(relation_type, start + '\0' + end), 'Unstable relation key')
        triple = (relation_type, start, end)
        require(triple not in triples and edge['id'] not in edge_ids, 'Duplicate relation')
        require(edge.get('medical_review_status') == 'not_verified', 'Unapproved medical certification')
        require(isinstance(edge.get('sources'), list) and edge['sources'], 'Missing relation evidence')
        for proof in edge['sources']:
            field, value, disease = check_proof(proof)
            expected = 'acompany_with' if field == 'acompany' else builder.FIELDS.get(field, (None, None))[1]
            require(expected == relation_type and isinstance(value, str) and value.strip() == lookup[end]['name']
                    and disease == lookup[start]['name'], 'Relation source mapping mismatch')
        triples.add(triple)
        edge_ids.add(edge['id'])
        used.update((start, end))
        outgoing[start].append(edge)
    for disease in (n for n in nodes if n['type'] == 'Disease'):
        own_edges = outgoing[disease['id']]
        if not full:
            require({rel for _, rel in builder.FIELDS.values()} <= {edge['type'] for edge in own_edges}, 'Missing required disease relationship category')
        foods = {rel: {edge['end_id'] for edge in own_edges if edge['type'] == rel} for rel in ('do_eat', 'no_eat')}
        require(not foods['do_eat'] & foods['no_eat'], 'Contradictory food relations')
    require(set(lookup) == used, 'Isolated entities')
    require(stats.get('graph', {}).get('entities') == len(nodes) and stats.get('graph', {}).get('relations') == len(edges), 'Statistics disagree with package')
    require(len(samples) == (stats.get('diseases') if full else len(stats.get('selected_diseases', []))), 'Disease count disagrees with report')
    return {'nodes': nodes, 'edges': edges, 'stats': stats,
            'file_hashes': {name: builder.digest(directory / name) for name in CORE_FILES}}


def node_row(node):
    return {'id': node['id'], 'name': node['name'], 'entity_type': node['type'],
            'sources_json': canonical(node['sources']), 'properties_json': canonical(node.get('properties', {})),
            'property_sources_json': canonical(node.get('property_sources', {})),
            'medical_review_status': node['medical_review_status'], 'payload_sha256': sha(node)}


def edge_row(edge):
    return {**{key: edge[key] for key in ('id', 'start_id', 'end_id', 'medical_review_status')},
            'sources_json': canonical(edge['sources']), 'payload_sha256': sha(edge)}


def relation_query(relation_type):
    require(relation_type in REL_TYPES, 'Relation type not allowed')
    # Only fixed allowlisted schema identifiers are interpolated; every data value is a parameter.
    return '''UNWIND $rows AS row
MATCH (a:CuratedEntity {dataset_id:$dataset_id, id:row.start_id})
MATCH (b:CuratedEntity {dataset_id:$dataset_id, id:row.end_id})
MERGE (a)-[r:''' + relation_type + ''' {dataset_id:$dataset_id, id:row.id}]->(b)
ON CREATE SET r.sources_json=row.sources_json, r.medical_review_status=row.medical_review_status,
 r.payload_sha256=row.payload_sha256
RETURN count(r) AS processed'''


def build_plan(package, batch_size=500, uri=None, database=None):
    require(type(batch_size) is int and 1 <= batch_size <= 1000, 'Batch size must be 1..1000')
    if uri:
        parsed = urlsplit(uri)
        require(parsed.scheme in {'bolt', 'bolt+s', 'bolt+ssc', 'neo4j', 'neo4j+s', 'neo4j+ssc'}
                and parsed.hostname and not parsed.username and not parsed.password and not parsed.query
                and not parsed.fragment, 'Invalid Neo4j URI (credentials must not be embedded)')
    if database:
        require(re.fullmatch(r'[A-Za-z0-9_.-]+', database), 'Invalid database name')
    grouped = defaultdict(list)
    for edge in package['edges']:
        grouped[edge['type']].append(edge_row(edge))
    node_batches = list(batches([node_row(n) for n in package['nodes']], batch_size))
    relationship_batches = [{'type': kind, 'rows': rows} for kind in sorted(grouped) for rows in batches(grouped[kind], batch_size)]
    binding = {'dataset_id': DATASET, 'input_file_sha256': package['file_hashes'],
               'batch_size': batch_size, 'target_uri': uri, 'target_database': database,
               'storage_contract': 'CuratedEntity-v1-json-evidence', 'transaction': 'one_atomic_data_transaction',
               'importer_sha256': builder.digest(Path(__file__))}
    report = {**binding, 'plan_sha256': sha(binding), 'mode': 'offline_dry_run', 'database_access': False,
              'data_validation': 'passed', 'entity_label': ENTITY_LABEL,
              'entities': len(package['nodes']), 'relations': len(package['edges']),
              'diseases': sum(n['type'] == 'Disease' for n in package['nodes']),
              'entities_by_type': dict(sorted(Counter(n['type'] for n in package['nodes']).items())),
              'relations_by_type': dict(sorted(Counter(e['type'] for e in package['edges']).items())),
              'entity_batches': len(node_batches), 'relation_batches': len(relationship_batches),
              'proposed_dataset_marker_nodes': 1,
              'proposed_constraints': ['curated_entity_key', 'curated_dataset_key'],
              'database_changes': {'new_entities': None, 'matched_entities': None, 'new_relations': None,
                                   'matched_relations': None, 'conflicts': None, 'deletions': 0,
                                   'note': 'Not checked against a live target; new/matched/conflict counts are unknown.'},
              'source_skipped_items': package['stats']['graph']['skipped_items'],
              'import_skipped_items': 0, 'failures': 0,
              'medical_review_status': 'not_verified', 'manual_review_status': package['stats']['manual_review_status'],
              'write_status': 'not_authorized_or_executed'}
    return {'report': report, 'node_batches': node_batches, 'relation_batches': relationship_batches}


def read_target(tx, dataset_id):
    marker = list(tx.run('MATCH (m:CuratedDataset {dataset_id:$dataset_id}) RETURN m.fingerprint AS fingerprint', dataset_id=dataset_id))
    nodes = [dict(row) for row in tx.run('MATCH (n:CuratedEntity {dataset_id:$dataset_id}) '
              'RETURN n.id AS id, n.name AS name, n.entity_type AS entity_type, n.payload_sha256 AS payload_sha256, '
              'n.sources_json AS sources_json, n.properties_json AS properties_json, '
              'n.property_sources_json AS property_sources_json, n.medical_review_status AS medical_review_status', dataset_id=dataset_id)]
    edges = [dict(row) for row in tx.run('MATCH (a)-[r]->(b) WHERE r.dataset_id=$dataset_id '
              'RETURN r.id AS id, type(r) AS type, a.id AS start_id, b.id AS end_id, '
              'a.dataset_id AS start_dataset, b.dataset_id AS end_dataset, r.payload_sha256 AS payload_sha256, '
              'r.sources_json AS sources_json, r.medical_review_status AS medical_review_status', dataset_id=dataset_id)]
    return {'markers': [dict(row) for row in marker], 'nodes': nodes, 'edges': edges}


def compare_target(package, target):
    desired_nodes = {n['id']: n for n in package['nodes']}
    desired_edges = {e['id']: e for e in package['edges']}
    conflicts, seen_nodes, seen_edges = [], set(), set()
    for item in target['nodes']:
        key = item['id']
        if key in seen_nodes or key not in desired_nodes:
            conflicts.append({'kind': 'entity', 'id': key, 'reason': 'duplicate_or_unknown_key'})
        else:
            expected = node_row(desired_nodes[key])
            if any(item.get(field) != value for field, value in expected.items()):
                conflicts.append({'kind': 'entity', 'id': key, 'reason': 'payload_or_identity_mismatch'})
        seen_nodes.add(key)
    for item in target['edges']:
        key = item['id']
        if key in seen_edges or key not in desired_edges:
            conflicts.append({'kind': 'relation', 'id': key, 'reason': 'duplicate_or_unknown_key'})
        else:
            expected = desired_edges[key]
            if (any(item.get(field) != value for field, value in edge_row(expected).items()) or item.get('type') != expected['type']
                    or item['start_dataset'] != DATASET or item['end_dataset'] != DATASET):
                conflicts.append({'kind': 'relation', 'id': key, 'reason': 'payload_or_endpoint_mismatch'})
        seen_edges.add(key)
    return {'new_entities': len(set(desired_nodes) - seen_nodes), 'matched_entities': len(set(desired_nodes) & seen_nodes),
            'new_relations': len(set(desired_edges) - seen_edges), 'matched_relations': len(set(desired_edges) & seen_edges),
            'conflicts': conflicts, 'deletions': 0}


def validate_marker(target, fingerprint):
    require(len(target['markers']) <= 1, 'Duplicate dataset marker')
    if target['markers']:
        require(target['markers'][0]['fingerprint'] == fingerprint, 'Dataset namespace belongs to another input fingerprint')
    else:
        require(not target['nodes'] and not target['edges'], 'Unmanaged records already exist in this dataset namespace')


def postcheck(tx, package):
    target = read_target(tx, DATASET)
    validate_marker(target, sha(package['file_hashes']))
    comparison = compare_target(package, target)
    require(not comparison['conflicts'] and comparison['new_entities'] == 0 and comparison['new_relations'] == 0, 'Post-import identity/content/count check failed')
    row = tx.run('''MATCH (n:CuratedEntity {dataset_id:$dataset_id})
RETURN sum(CASE WHEN n.name IS NULL OR trim(n.name)='' THEN 1 ELSE 0 END) AS empty_names,
 sum(CASE WHEN NOT EXISTS { MATCH (n)-[]-(:CuratedEntity {dataset_id:$dataset_id}) } THEN 1 ELSE 0 END) AS isolated_nodes,
 sum(CASE WHEN n.sources_json IS NULL OR n.sources_json='[]' THEN 1 ELSE 0 END) AS missing_sources''', dataset_id=DATASET).single()
    require(row is not None and all(row[k] == 0 for k in ('empty_names', 'isolated_nodes', 'missing_sources')), 'Post-import empty/isolation/source check failed')
    return {'entities': len(target['nodes']), 'relations': len(target['edges']), 'empty_names': row['empty_names'],
            'isolated_nodes': row['isolated_nodes'], 'missing_sources': row['missing_sources'],
            'duplicate_entities': 0, 'duplicate_relations': 0, 'missing_endpoints': 0,
            'identity_and_payload': 'passed'}


def import_transaction(tx, package, plan, run_id):
    """All node and edge batches commit together; errors roll back the entire data transaction."""
    fingerprint = sha(package['file_hashes'])
    marker = tx.run('''MERGE (m:CuratedDataset {dataset_id:$dataset_id})
ON CREATE SET m.fingerprint=$fingerprint
SET m.last_import_run=$run_id
RETURN m.fingerprint AS fingerprint''', dataset_id=DATASET, fingerprint=fingerprint, run_id=run_id).single()
    require(marker and marker['fingerprint'] == fingerprint, 'Dataset namespace fingerprint conflict')
    before = read_target(tx, DATASET)
    validate_marker(before, fingerprint)
    comparison = compare_target(package, before)
    require(not comparison['conflicts'], 'Existing curated records conflict; no overwrite permitted')
    for rows in plan['node_batches']:
        result = tx.run(NODE_QUERY, rows=rows, dataset_id=DATASET).single()
        require(result and result['processed'] == len(rows), 'Entity batch count mismatch')
    for batch in plan['relation_batches']:
        result = tx.run(relation_query(batch['type']), rows=batch['rows'], dataset_id=DATASET).single()
        require(result and result['processed'] == len(batch['rows']), 'Relation batch count mismatch')
    checks = postcheck(tx, package)
    return {'changes': comparison, 'post_import_validation': checks, 'import_skipped_items': 0}


def check_apply_permission(args, report):
    require(args.uri and args.database, 'Apply requires an explicit target URI and database')
    require(args.confirm == report['plan_sha256'], 'Apply requires confirmation of the exact target-bound preview fingerprint')
    require(args.review_file is not None, 'Apply requires a review record for this exact plan')
    try:
        review = json.loads(args.review_file.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ImportProblem('Cannot read review record') from error
    require(isinstance(review, dict) and review.get('plan_sha256') == report['plan_sha256']
            and review.get('engineering_demo_review') == 'approved'
            and review.get('database_write_authorized') is True,
            'Review must approve this exact engineering demo and explicitly authorize database writing')
    backup_hash = None
    if args.backup_ref is not None:
        require(args.backup_ref.is_file() and args.backup_ref.stat().st_size > 0,
                'Supplied backup reference must be an existing nonempty artifact')
        require(args.backup_ref.name != '.env', 'Environment file is not a backup reference')
        backup_hash = builder.digest(args.backup_ref)
    return {'backup_reference_sha256': backup_hash, 'review_record_sha256': builder.digest(args.review_file)}


def connect(uri):
    # Imported and configuration loaded only after an explicit online mode is selected.
    import os
    from dotenv import dotenv_values
    from neo4j import GraphDatabase
    config = dotenv_values(builder.PROJECT / 'knowledge_graph_backend' / '.env')
    user = os.environ.get('NEO4J_USERNAME') or config.get('NEO4J_USERNAME')
    password = os.environ.get('NEO4J_PASSWORD') or config.get('NEO4J_PASSWORD')
    require(user and password, 'Neo4j credentials are not configured')
    return GraphDatabase.driver(uri, auth=(user, password), connection_timeout=10)


def online_run(package, plan, args, driver_factory=connect):
    fingerprint = sha(package['file_hashes'])
    approval = check_apply_permission(args, plan['report']) if args.apply else {}
    require(args.uri and args.database, 'Target inspection requires an explicit URI and database')
    from neo4j import READ_ACCESS
    with driver_factory(args.uri) as driver:
        with driver.session(database=args.database, default_access_mode=READ_ACCESS) as session:
            target = session.execute_read(read_target, DATASET)
        validate_marker(target, fingerprint)
        changes = compare_target(package, target)
        result = {**plan['report'], 'mode': 'apply' if args.apply else 'read_only_target_preview',
                  'database_access': True, 'database_changes': changes}
        if not args.apply:
            return result
        require(not changes['conflicts'], 'Read-only target preflight found conflicts')
        with driver.session(database=args.database) as session:
            # Schema changes happen only in explicitly authorized apply mode.
            session.run(NODE_CONSTRAINT).consume()
            session.run(MARKER_CONSTRAINT).consume()
            constraints = list(session.run('SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties '
                           'WHERE name IN $names RETURN name, type, labelsOrTypes, properties',
                           names=['curated_entity_key', 'curated_dataset_key']))
            expected = {'curated_entity_key': (['CuratedEntity'], ['dataset_id', 'id']),
                        'curated_dataset_key': (['CuratedDataset'], ['dataset_id'])}
            require(len(constraints) == 2 and {row['name'] for row in constraints} == set(expected) and all(row['name'] in expected and row['type'] == 'UNIQUENESS'
                    and (row['labelsOrTypes'], row['properties']) == expected[row['name']] for row in constraints),
                    'Constraint names exist but definitions do not match; refusing data write')
            # Re-check inside the same transaction after acquiring the dataset marker lock.
            imported = session.execute_write(import_transaction, package, plan, str(uuid.uuid4()))
            # Separate read after commit checks durable state.
            verified = session.execute_read(postcheck, package)
        result.update(imported)
        result.update(approval)
        result['database_changes'] = imported['changes']
        result['post_commit_validation'] = verified
        result['write_status'] = 'committed_and_verified'
        result['schema_constraints'] = ['curated_entity_key', 'curated_dataset_key']
        return result


def report_destination(path):
    path = Path(path).resolve()
    require(path.is_relative_to((builder.WORKSPACE / 'temp').resolve()) and path.suffix == '.json',
            'Report must be a JSON file under workspace temp/')
    require(not path.exists(), 'Report already exists; choose a new filename')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=builder.DEFAULT_OUTPUT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--inspect-target', action='store_true', help='Explicit read-only live comparison')
    mode.add_argument('--apply', action='store_true', help='Requires target-bound confirmation and review; backup reference is optional')
    parser.add_argument('--uri')
    parser.add_argument('--database')
    parser.add_argument('--batch-size', type=int, default=500)
    parser.add_argument('--confirm')
    parser.add_argument('--review-file', type=Path)
    parser.add_argument('--backup-ref', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    started = time.monotonic()
    destination = None
    apply_attempted = False
    result = {'mode': 'apply' if args.apply else 'preview', 'status': 'failed', 'failures': 1,
              'write_status': 'not_started', 'deletions': 0}
    exit_code = 0
    try:
        if args.report:
            destination = report_destination(args.report)
        package = validate_dataset(args.data_dir)
        plan = build_plan(package, args.batch_size, args.uri, args.database)
        if args.apply:
            check_apply_permission(args, plan['report'])
            apply_attempted = True
        if args.apply or args.inspect_target:
            result = online_run(package, plan, args)
        else:
            result = plan['report']
        result['status'] = 'passed'
    except ImportProblem as error:
        result['error'] = str(error)
        if apply_attempted:
            result['write_status'] = 'apply_failed_check_target_before_retry'
            result['note'] = 'Data transaction errors roll back; schema changes or a completed commit may remain. Inspect target before retry.'
        exit_code = 2
    except Exception as error:
        # Never emit driver exception text, connection config, or traceback (may contain credentials).
        result['error'] = 'Operation failed: ' + type(error).__name__
        result['write_status'] = 'unknown_check_target_before_retry' if args.apply else 'not_started'
        exit_code = 1
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    if destination:
        with destination.open('x', encoding='utf-8') as stream:
            stream.write(encoded)
    print(encoded)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
