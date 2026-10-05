"""Offline, deterministic source-preserving demo dataset builder. No database code."""
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parent
SOURCE = PROJECT / 'data' / 'medical.json'
POLICY = Path(__file__).with_name('curated_selection_v1.json')
DEFAULT_OUTPUT = PROJECT / 'data' / 'curated_v1'
FIELDS = {
    'symptom': ('Symptom', 'has_symptom'),
    'check': ('Check', 'need_check'),
    'cure_department': ('Department', 'belongs_to'),
    'cure_way': ('Cure', 'cure_way'),
    'common_drug': ('Drug', 'common_drug'),
    'recommand_drug': ('Drug', 'recommand_drug'),
    'do_eat': ('Food', 'do_eat'),
    'not_eat': ('Food', 'no_eat'),
    'recommand_eat': ('Food', 'recommand_eat'),
}
TEXT_FIELDS = ('desc', 'cause', 'prevent')
PLACEHOLDERS = {'无', '暂无', '不详', '未知', '无资料', '暂无资料', 'null', 'None', '-', '--'}


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def source_fingerprints(source=SOURCE):
    """Rehash live raw source; retain explicitly historical hashes for archived outputs."""
    hashes = {source.name: digest(source)}
    if source.resolve() == SOURCE.resolve():
        record = json.loads((SOURCE.parent / 'legacy_source_fingerprints.json').read_text(encoding='utf-8'))
        saved = record['source_file_sha256']
        if saved['medical.json'] != hashes[source.name]:
            raise ValueError('Raw source differs from the recorded dataset provenance')
        hashes.update({name: value for name, value in saved.items() if name != 'medical.json'})
    for name in ('entities.json', 'relations.json'):
        path = source.with_name(name)
        if path.is_file():
            hashes[name] = digest(path)
    return hashes


def identifier(kind, name):
    return kind + ':' + hashlib.sha256((kind + '\0' + name).encode('utf-8')).hexdigest()


def bad_name(value):
    if not isinstance(value, str):
        return 'non_string'
    text = value.strip()
    if not text:
        return 'empty'
    if text in PLACEHOLDERS:
        return 'placeholder'
    if '...' in text or '…' in text:
        return 'truncated'
    if '\ufffd' in text or any(ord(c) < 32 for c in text):
        return 'replacement_or_control_character'
    if len(text) > 80:
        return 'overlong_label'
    return None


def clean_list(record, field):
    raw = record.get(field)
    if raw is None:
        return [], [{'field': field, 'reason': 'missing_or_null'}]
    if not isinstance(raw, list):
        return [], [{'field': field, 'reason': 'wrong_field_type', 'raw_value': raw}]
    if not raw:
        return [], [{'field': field, 'reason': 'empty_list'}]
    values, issues, seen = [], [], set()
    for index, value in enumerate(raw):
        reason = bad_name(value)
        if reason:
            issues.append({'field': field, 'index': index, 'raw_value': value, 'reason': reason})
            continue
        name = value.strip()
        if name in seen:
            issues.append({'field': field, 'index': index, 'raw_value': value, 'reason': 'duplicate_value'})
            continue
        seen.add(name)
        values.append((name, index, value))
    return values, issues


def load_records(path):
    records, errors = [], []
    lines = path.read_text(encoding='utf-8-sig').splitlines()
    for line, text in enumerate(lines, 1):
        if not text.strip():
            errors.append({'line': line, 'reason': 'blank_line'})
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError:
            errors.append({'line': line, 'reason': 'invalid_json'})
            continue
        if not isinstance(record, dict):
            errors.append({'line': line, 'reason': 'non_object_record'})
            continue
        records.append((line, record))
    return records, errors, len(lines)


def eligibility(record, duplicate_names):
    reasons = []
    name = record.get('name')
    if bad_name(name):
        reasons.append('invalid_disease_name')
    elif name.strip() in duplicate_names:
        reasons.append('duplicate_disease_name_requires_manual_resolution')
    for field in TEXT_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or len(value.strip()) < 20 or '\ufffd' in value:
            reasons.append('incomplete_text:' + field)
    clean, issues = {}, []
    for field in FIELDS:
        clean[field], found = clean_list(record, field)
        issues.extend(found)
        if not clean[field]:
            reasons.append('no_usable_values:' + field)
    conflict = sorted({v[0] for v in clean['do_eat']} & {v[0] for v in clean['not_eat']})
    if conflict:
        reasons.append('contradictory_food:' + ','.join(conflict))
    return reasons, clean, issues


def make_package(source=SOURCE, policy_path=POLICY):
    policy = json.loads(policy_path.read_text(encoding='utf-8'))
    records, parse_errors, physical_lines = load_records(source)
    names = Counter(r['name'].strip() for _, r in records if not bad_name(r.get('name')))
    duplicates = {n for n, count in names.items() if count > 1}
    analyses = {}
    anomalies = []
    for line, record in records:
        reasons, cleaned, issues = eligibility(record, duplicates)
        analyses[line] = (reasons, cleaned)
        anomalies.extend({'line': line, 'name': record.get('name'), **i} for i in issues)
    by_name = {r['name'].strip(): (line, r) for line, r in records if not bad_name(r.get('name'))}
    selected, candidate_decisions = [], []
    for group in policy['groups']:
        chosen = 0
        for name in group['candidates']:
            item = by_name.get(name)
            reasons = ['not_in_source'] if item is None else analyses[item[0]][0]
            status = 'excluded_quality' if reasons else 'not_selected_quota'
            if not reasons and chosen < group['quota']:
                line, record = item
                selected.append((group['name'], line, record))
                chosen += 1
                status = 'selected'
            candidate_decisions.append({'group': group['name'], 'name': name, 'status': status,
                                        'reasons': reasons, 'line': item[0] if item else None})
        if chosen != group['quota']:
            raise ValueError('Insufficient eligible candidates: ' + group['name'])
    selected_names = {r['name'].strip() for _, _, r in selected}
    if len(selected_names) != len(selected):
        raise ValueError('Disease selected more than once')
    hashes = source_fingerprints(source)
    entities, relations = {}, {}
    sample_records, skipped = [], []
    raw_relation_count = 0

    def evidence(line, record, field, raw_value, index=None):
        result = {'file': 'bishe/data/medical.json', 'file_sha256': hashes[source.name],
                  'line': line, 'record_id': record.get('_id'), 'disease': record['name'],
                  'field': field, 'raw_value': raw_value}
        if index is not None:
            result['index'] = index
        return result

    def add_entity(kind, name, proof, properties=None):
        key = identifier(kind, name)
        if key not in entities:
            entities[key] = {'id': key, 'type': kind, 'name': name, 'sources': [],
                             'medical_review_status': 'not_verified'}
        if proof not in entities[key]['sources']:
            entities[key]['sources'].append(proof)
        if properties:
            entities[key]['properties'] = properties
        return key

    def add_relation(start, end, relation_type, proof):
        nonlocal raw_relation_count
        raw_relation_count += 1
        key = identifier(relation_type, start + '\0' + end)
        if key not in relations:
            relations[key] = {'id': key, 'type': relation_type, 'start_id': start, 'end_id': end,
                              'sources': [], 'medical_review_status': 'not_verified'}
        relations[key]['sources'].append(proof)

    for group, line, record in selected:
        cleaned = analyses[line][1]
        name = record['name'].strip()
        properties = {field: record[field] for field in TEXT_FIELDS}
        disease = add_entity('Disease', name, evidence(line, record, 'name', record['name']), properties)
        entities[disease]['property_sources'] = {field: evidence(line, record, field, record[field]) for field in TEXT_FIELDS}
        sample_records.append({'group': group, 'source': evidence(line, record, 'name', record['name']),
                               'raw_record': record, 'review_status': 'pending_manual_review'})
        for field, (kind, relation_type) in FIELDS.items():
            for value, index, raw_value in cleaned[field]:
                proof = evidence(line, record, field, raw_value, index)
                endpoint = add_entity(kind, value, proof)
                add_relation(disease, endpoint, relation_type, proof)
        acompanies, issues = clean_list(record, 'acompany')
        skipped.extend({'line': line, 'name': name, **i} for i in issues)
        for value, index, raw_value in acompanies:
            if value == name or value not in selected_names:
                skipped.append({'line': line, 'name': name, 'field': 'acompany', 'index': index,
                                'raw_value': raw_value, 'reason': 'self_reference' if value == name else 'disease_outside_sample'})
                continue
            add_relation(disease, identifier('Disease', value), 'acompany_with', evidence(line, record, 'acompany', raw_value, index))
        for index, value in enumerate(record.get('drug_detail', [])):
            skipped.append({'line': line, 'name': name, 'field': 'drug_detail', 'index': index,
                            'raw_value': value, 'reason': 'unverified_product_details_not_in_graph'})
    selected_lines = {line for _, line, _ in selected}
    skipped.extend(a for a in anomalies if a['line'] in selected_lines)
    entity_list = sorted(entities.values(), key=lambda x: (x['type'], x['name']))
    relation_list = sorted(relations.values(), key=lambda x: (x['type'], x['start_id'], x['end_id']))
    keys = set(entities)
    dangling = sum(r['start_id'] not in keys or r['end_id'] not in keys for r in relation_list)
    used = {r[k] for r in relation_list for k in ('start_id', 'end_id')}
    validations = {'missing_endpoints': dangling,
                   'empty_entity_names': sum(not e['name'].strip() for e in entity_list),
                   'duplicate_entity_keys': len(entity_list) - len({(e['type'], e['name']) for e in entity_list}),
                   'duplicate_relation_keys': len(relation_list) - len({(r['type'], r['start_id'], r['end_id']) for r in relation_list}),
                   'entities_without_sources': sum(not e['sources'] for e in entity_list),
                   'relations_without_sources': sum(not r['sources'] for r in relation_list),
                   'isolated_entities': len(keys - used),
                   'producer_entities': sum(e['type'] == 'Producer' for e in entity_list),
                   'drugs_of_relations': sum(r['type'] == 'drugs_of' for r in relation_list)}
    if any(validations.values()):
        raise ValueError('Invalid graph: ' + str(validations))
    all_fields = sorted({k for _, r in records for k in r})
    field_stats = {field: {'missing': sum(field not in r for _, r in records),
                           'null': sum(field in r and r[field] is None for _, r in records),
                           'empty': sum(field in r and r[field] in ('', [], {}) for _, r in records),
                           'types': dict(sorted(Counter(type(r[field]).__name__ for _, r in records if field in r).items()))}
                   for field in all_fields}
    audit = [{'line': line, 'name': r.get('name'), 'status': 'selected' if line in selected_lines else
              ('excluded_quality' if analyses[line][0] else 'outside_demo_scope'),
              'reasons': analyses[line][0]} for line, r in records]
    report = {'dataset_version': policy['version'], 'source_file_sha256': hashes,
              'source': {'physical_lines': physical_lines, 'parsed_records': len(records),
                         'unique_usable_names': len(names), 'duplicate_names': sorted(duplicates),
                         'parse_errors': parse_errors, 'field_statistics': field_stats,
                         'eligibility_pass': sum(not reasons for reasons, _ in analyses.values()),
                         'eligibility_fail': sum(bool(reasons) for reasons, _ in analyses.values()),
                         'status_counts': dict(Counter(a['status'] for a in audit)),
                         'quality_failure_reasons': dict(sorted(Counter(reason for reasons, _ in analyses.values() for reason in reasons).items())),
                         'list_anomaly_reasons': dict(sorted(Counter(a['reason'] for a in anomalies).items()))},
              'selected_diseases': [{'group': g, 'name': r['name'], 'line': line} for g, line, r in selected],
              'candidate_decisions': candidate_decisions,
              'graph': {'entities': len(entity_list), 'entities_by_type': dict(sorted(Counter(e['type'] for e in entity_list).items())),
                        'relations_before_deduplication': raw_relation_count,
                        'relations': len(relation_list), 'relations_by_type': dict(sorted(Counter(r['type'] for r in relation_list).items())),
                        'duplicate_relations_removed': raw_relation_count - len(relation_list),
                        'skipped_items': len(skipped), 'skip_reasons': dict(sorted(Counter(s['reason'] for s in skipped).items())),
                        'validation': validations},
              'medical_review_status': 'not_verified', 'manual_review_status': 'pending',
              'medical_reliability': 'Source fidelity and structural checks only; no clinical correctness certification.',
              'database_access': 'none'}
    review = ['# curated_v1 人工检查清单', '',
              '本数据用于工程演示。已核验来源追溯和图结构，未核验医学正确性；所有项目待人工检查。',
              '药品、饮食和治疗关系仅表示原始文件中的记载，不构成诊断、处方或饮食建议。', '',
              '| 演示分组 | 疾病 | 原始行号 |', '| --- | --- | --- |']
    review += [f'| {g} | {r["name"]} | {line} |' for g, line, r in selected]
    labels = {'symptom': '症状', 'check': '检查', 'cure_department': '科室（保留原有顺序，不推断层级）',
              'cure_way': '治疗方式', 'common_drug': '原始常用药品', 'recommand_drug': '原始推荐药品',
              'do_eat': '原始宜吃食物', 'not_eat': '原始忌吃食物', 'recommand_eat': '原始食谱'}
    for group, line, record in selected:
        review += ['', '## ' + record['name'], '', f'分组：{group}；原始文件行号：{line}；记录标识：{record.get("_id")}', '',
                   '- [ ] 核查疾病、症状、检查和治疗是否匹配', '- [ ] 核查药品和食物条目是否可信',
                   '- [ ] 核查描述、病因和预防文字是否有冲突或过时内容', '']
        for field in FIELDS:
            review.append(labels[field] + '：' + '、'.join(x[0] for x in analyses[line][1][field]))
            review.append('')
        review += ['删除或跳过的条目：', '']
        for item in skipped:
            if item['line'] == line and item['reason'] != 'unverified_product_details_not_in_graph':
                review.append(f'- {item["field"]}：{item.get("raw_value", "")}（{item["reason"]}）')
        for field, label in [('desc', '疾病描述'), ('cause', '病因'), ('prevent', '预防')]:
            review += ['', '### 原始' + label, '', record[field]]
    return {'entities.json': encode(entity_list), 'relations.json': encode(relation_list),
            'source_records.json': encode(sample_records), 'statistics.json': encode(report),
            'source_audit.json': encode({'records': audit, 'list_anomalies': anomalies, 'parse_errors': parse_errors}),
            'skipped_items.json': encode(skipped), 'selection_rules.json': encode(policy),
            '人工检查清单.md': ('\n'.join(review) + '\n').encode('utf-8')}, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--verify-existing', action='store_true', help='Rebuild in memory and compare; write nothing')
    args = parser.parse_args()
    target = args.output.resolve()
    if target != DEFAULT_OUTPUT.resolve() and not target.is_relative_to((WORKSPACE / 'temp').resolve()):
        parser.error('Output must be curated_v1 or under workspace temp/')
    package, report = make_package()
    if args.verify_existing:
        for name, content in package.items():
            if (target / name).read_bytes() != content:
                raise ValueError('Reproducibility check failed: ' + name)
        print('PASS: all generated files are byte-identical; no writes')
    else:
        if target.exists():
            parser.error('Output already exists; refusing overwrite. Use --verify-existing or a new temp/ directory.')
        target.mkdir(parents=True)
        for name, content in package.items():
            with (target / name).open('xb') as stream:
                stream.write(content)
        print(json.dumps({'diseases': len(report['selected_diseases']), 'graph': report['graph'], 'output': str(target)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
