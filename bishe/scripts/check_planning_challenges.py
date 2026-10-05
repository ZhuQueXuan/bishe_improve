"""Freeze/verify source-backed planning tasks. Default is offline; no model API.

--freeze creates a NEW bundle. --read-only checks existing graph responses,
without installing a planner or evaluating model quality.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parent
BUNDLE = PROJECT / 'data/planning_challenge_v1'
SOURCE = PROJECT / 'data/medical.json'
CONFIG = [
    ('P01', 'dev', '糖尿病', 'normal'),
    ('P02', 'dev', '胃溃疡', 'normal'),
    ('P03', 'holdout', '冠心病', 'normal'),
    ('P04', 'holdout', '急性胰腺炎', 'normal'),
    ('P05', 'dev', '高血压', 'missing_prevent'),
    ('P06', 'holdout', '肺炎', 'transient_error'),
    ('P07', 'dev', '贫血', 'resume_after_relations'),
    ('P08', 'holdout', '感冒', 'resume_after_relations'),
]


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def array_items(path):
    """Read a large top-level JSON array without loading its whole contents."""
    decoder = json.JSONDecoder()
    with path.open(encoding='utf-8') as stream:
        buffer = stream.read(1024 * 1024).lstrip()
        if not buffer.startswith('['):
            raise ValueError('Expected JSON array')
        buffer = buffer[1:]
        while True:
            buffer = buffer.lstrip()
            if buffer.startswith(']'):
                return
            if buffer.startswith(','):
                buffer = buffer[1:].lstrip()
            try:
                item, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                more = stream.read(1024 * 1024)
                if not more:
                    raise ValueError('Incomplete array')
                buffer += more
                continue
            yield item
            buffer = buffer[end:]


def write_new(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def freeze():
    if BUNDLE.exists():
        raise ValueError('Frozen bundle exists; create a new version rather than overwrite')
    entities = {x['id']: x for x in array_items(PROJECT / 'data/full_v1/entities.json')
                if x['type'] == 'Disease'}
    by_name = {x['name']: x for x in entities.values()}
    seeds = {by_name[name]['id'] for _, _, name, _ in CONFIG}
    neighbors = {key: [] for key in seeds}
    for rel in array_items(PROJECT / 'data/full_v1/relations.json'):
        if rel['type'] == 'acompany_with' and rel['start_id'] in seeds:
            neighbors[rel['start_id']].append(rel)
    tasks, oracle = [], []
    for task_id, split, name, scenario in CONFIG:
        seed = by_name[name]
        edges = sorted(neighbors[seed['id']], key=lambda r: entities[r['end_id']]['name'])[:8]
        chosen = edges[:2]
        if not chosen:
            raise ValueError('No reachable objects: ' + name)
        question = (f'只做资料整理：先查{name}的并发疾病原文关系。在工具按名称Unicode升序返回的最多8个对象中，'
                    '选前2个（不足2个则全部），分别列出疾病描述和预防原文摘录及出处。'
                    '这是并列表格，不要求推断医学异同；缺失资料请明确标出，不推断我患病。')
        task = {'id': task_id, 'split': split, 'question': question, 'seed': {'id': seed['id'], 'name': name},
                'scenario': scenario, 'selection': {'order': 'unicode_name_ascending', 'visible_limit': 8, 'take': 2},
                'required_fields': ['desc', 'prevent'],
                'budget': {'read_calls': 5, 'planner_decisions': 4, 'answer_calls': 1, 'total_seconds': 90}}
        if scenario == 'missing_prevent':
            task['fixture'] = {'event': 'first_selected_entity.prevent', 'effect': 'return_missing', 'scope': 'local_tool_return_only'}
        elif scenario == 'transient_error':
            task['fixture'] = {'event': 'first_read_fields_call', 'effect': 'transient_error_once', 'scope': 'local_tool_return_only'}
        elif scenario == 'resume_after_relations':
            task['fixture'] = {'event': 'after_read_relations', 'effect': 'checkpoint_and_new_controller', 'scope': 'isolated_task_state'}
        expected = []
        for position, edge in enumerate(chosen):
            entity = entities[edge['end_id']]
            fields = []
            for field in task['required_fields']:
                value = entity.get('properties', {}).get(field, '')
                proof = entity.get('property_sources', {}).get(field)
                proofs = [proof] if isinstance(proof, dict) else []
                masked = scenario == 'missing_prevent' and position == 0 and field == 'prevent'
                if not isinstance(value, str) or not value.strip() or not proofs:
                    raise ValueError('Choose a task with real text; missing is isolated injection')
                fields.append({'field': field, 'expected_status': 'missing' if masked else 'found',
                               'available_value': value[:1800], 'sources': proofs,
                               'truncated': len(value) > 1800})
            expected.append({'id': entity['id'], 'name': entity['name'], 'relation_id': edge['id'],
                             'relation_sources': edge['sources'], 'fields': fields})
        tasks.append(task)
        oracle.append({'id': task_id, 'visible_relations': [{'id': r['end_id'], 'name': entities[r['end_id']]['name'],
                       'relation_id': r['id'], 'sources': r['sources']} for r in edges],
                       'selected': expected, 'expected_stop': 'partial' if scenario == 'missing_prevent' else 'complete'})
    BUNDLE.mkdir()
    write_new(BUNDLE / 'tasks.json', {'version': 'planning_challenge_v1', 'tasks': tasks})
    write_new(BUNDLE / 'oracle.json', {'version': 'planning_challenge_v1', 'do_not_send_to_planner': True, 'cases': oracle})
    write_new(BUNDLE / 'manifest.json', {'version': 'planning_challenge_v1', 'created': '2026-10-04',
        'dataset_id': 'medical_full_v1', 'medical_review_status': 'not_verified',
        'hashes': {str(p.relative_to(WORKSPACE)).replace('\\', '/'): digest(p) for p in
                   [SOURCE, PROJECT / 'data/full_v1/entities.json', PROJECT / 'data/full_v1/relations.json', BUNDLE / 'tasks.json', BUNDLE / 'oracle.json']},
        'split': {'dev': ['P01', 'P02', 'P05', 'P07'], 'holdout': ['P03', 'P04', 'P06', 'P08']},
        'limitations': ['Small shared task family; not representative of general planning.',
                       'Holdout covers new seed entities but not an unseen task template.',
                       'Oracle proves source availability, not model or medical correctness.',
                       'Missing/failure/resume fixtures do not change the live database.']})


def verify_proof(proof, value, records, source_hash, field):
    assert proof['file'] == 'bishe/data/medical.json'
    assert proof['file_sha256'] == source_hash
    raw = records[proof['line']]
    assert raw['_id'] == proof['record_id']
    assert raw['name'].strip() == proof['disease']
    assert proof['field'] == field
    expected = raw[field]
    if 'index' in proof:
        expected = expected[proof['index']].strip()
    else:
        expected = expected[:1800]
    assert expected == value


def verify(read_only=False):
    manifest = json.loads((BUNDLE / 'manifest.json').read_text(encoding='utf-8'))
    for relative, expected in manifest['hashes'].items():
        assert digest(WORKSPACE / relative) == expected, relative
    tasks = json.loads((BUNDLE / 'tasks.json').read_text(encoding='utf-8'))['tasks']
    gold = {x['id']: x for x in json.loads((BUNDLE / 'oracle.json').read_text(encoding='utf-8'))['cases']}
    records = {i: json.loads(line) for i, line in enumerate(SOURCE.open(encoding='utf-8'), 1)}
    source_hash = digest(SOURCE)
    checked, proof_count = [], 0
    if read_only:
        sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'knowledge_graph_backend.settings')
        import django
        django.setup()
        from api.graph_retrieval import retrieve_context
    for task in tasks:
        expected = gold[task['id']]
        visible = expected['visible_relations']
        assert len(visible) <= 8
        assert [x['name'] for x in visible] == sorted(x['name'] for x in visible)
        assert [x['id'] for x in expected['selected']] == [x['id'] for x in visible[:2]]
        assert all(x['name'] not in task['question'] for x in expected['selected'])
        for edge in visible:
            for proof in edge['sources']:
                verify_proof(proof, edge['name'], records, source_hash, 'acompany')
                assert proof['disease'] == task['seed']['name']
                proof_count += 1
        for entity in expected['selected']:
            for field in entity['fields']:
                for proof in field['sources']:
                    verify_proof(proof, field['available_value'], records, source_hash, field['field'])
                    assert proof['disease'] == entity['name']
                    proof_count += 1
        if read_only:
            # Existing graph adapter only: this is source availability, not planner evaluation.
            result = retrieve_context(task['seed']['name'], [], ['acompany_with'], timeout=30)
            assert result['status'] == 'found'
            facts = result['facts']
            assert [f['value'] for f in facts] == [x['name'] for x in visible]
            for fact, edge in zip(facts, visible):
                assert fact['sources'][0] == {k: edge['sources'][0][k] for k in
                    ('file', 'file_sha256', 'line', 'record_id', 'disease', 'field', 'index') if k in edge['sources'][0]}
            for entity in expected['selected']:
                result = retrieve_context(entity['name'], [], ['desc', 'prevent'], timeout=30)
                assert result['diseases'] == [entity['name']]
                assert {(x['field'], x['value']) for x in result['facts']} == {
                    (x['field'], x['available_value']) for x in entity['fields']}
        checked.append({'id': task['id'], 'seed': task['seed']['name'], 'selected': [x['name'] for x in expected['selected']],
                        'scenario': task['scenario'], 'source_available': True})
    return {'status': 'passed', 'mode': 'read_only_availability' if read_only else 'offline_source_integrity',
            'cases': checked, 'source_proofs_verified': proof_count, 'external_model_calls': 0, 'database_writes': 0,
            'fixtures_executed': False, 'planner_evaluated': False,
            'limitations': ['Injected fault and checkpoint behavior are specified, not implemented or scored.',
                           'Availability checker reads oracle; it must not be presented as agent success.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--freeze', action='store_true')
    parser.add_argument('--read-only', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    account = PROJECT / 'knowledge_graph_backend/db.sqlite3'
    protected = {p: digest(p) for p in [SOURCE, account]}
    if args.freeze:
        freeze()
    result = verify(args.read_only)
    assert all(digest(p) == sha for p, sha in protected.items())
    result['protected_hashes_unchanged'] = {str(p.relative_to(WORKSPACE)).replace('\\', '/'): sha for p, sha in protected.items()}
    if args.report:
        output = args.report.resolve()
        if not output.is_relative_to(WORKSPACE / 'temp') or output.exists():
            raise ValueError('Report must be new and inside temp')
        write_new(output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
