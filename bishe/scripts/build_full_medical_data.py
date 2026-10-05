"""Build the full source-preserving engineering dataset; never connects to a database."""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import build_curated_data as core

OUTPUT = core.PROJECT / 'data/full_v1'


def make_package():
    rows, parse_errors, line_count = core.load_records(core.SOURCE)
    hashes = core.source_fingerprints()
    groups, excluded, issues = defaultdict(list), [], []
    for line, record in rows:
        name = record.get('name')
        if core.bad_name(name) or name.strip() in {'无标题文档', '未命名', '无标题'}:
            excluded.append({'line': line, 'raw_record': record, 'reason': 'invalid_or_placeholder_disease_name'})
            continue
        groups[name.strip()].append((line, record))

    def score(item):
        line, record = item
        lists = [core.clean_list(record, field)[0] for field in core.FIELDS]
        text_length = sum(len(record.get(field, '')) for field in core.TEXT_FIELDS if isinstance(record.get(field), str))
        return (sum(bool(values) for values in lists), sum(len(values) for values in lists), text_length, -line)

    selected, duplicates = [], []
    for name in sorted(groups):
        candidates = sorted(groups[name], key=score, reverse=True)
        selected.append(candidates[0])
        for item in candidates[1:]:
            line, record = item
            primary_line, primary = candidates[0]
            different = sorted(field for field in set(record) | set(primary) if record.get(field) != primary.get(field))
            duplicates.append({'name': name, 'selected_line': primary_line, 'excluded_line': line,
                               'different_fields': different, 'reason': 'duplicate_choose_complete_record_no_content_merge'})
            excluded.append({'line': line, 'raw_record': record, 'reason': 'duplicate_noncanonical_record', 'selected_line': primary_line})

    selected_names = {record['name'].strip() for _, record in selected}
    entities, relations, raw_samples = {}, {}, []
    primary_candidates = 0

    def proof(line, record, field, value, index=None):
        result = {'file': 'bishe/data/medical.json', 'file_sha256': hashes['medical.json'], 'line': line,
                  'record_id': record['_id'], 'disease': record['name'], 'field': field, 'raw_value': value}
        if index is not None:
            result['index'] = index
        return result

    def node(kind, name, source):
        key = core.identifier(kind, name)
        if key not in entities:
            entities[key] = {'id': key, 'type': kind, 'name': name, 'sources': [], 'medical_review_status': 'not_verified'}
        entities[key]['sources'].append(source)
        return key

    def edge(start, end, kind, source):
        key = core.identifier(kind, start + '\0' + end)
        if key not in relations:
            relations[key] = {'id': key, 'type': kind, 'start_id': start, 'end_id': end,
                              'sources': [], 'medical_review_status': 'not_verified'}
        relations[key]['sources'].append(source)

    for line, record in selected:
        name = record['name'].strip()
        disease = node('Disease', name, proof(line, record, 'name', record['name']))
        raw_samples.append({'source': proof(line, record, 'name', record['name']), 'raw_record': record,
                            'review_status': 'pending_manual_review'})
        properties, property_sources = {}, {}
        for field in core.TEXT_FIELDS:
            value = record.get(field)
            if isinstance(value, str) and value.strip() and value.strip() not in core.PLACEHOLDERS and '\ufffd' not in value:
                properties[field] = value
                property_sources[field] = proof(line, record, field, value)
            else:
                issues.append({'line': line, 'name': name, 'field': field, 'raw_value': value, 'reason': 'missing_or_invalid_text_not_filled'})
        entities[disease]['properties'] = properties
        entities[disease]['property_sources'] = property_sources
        cleaned = {}
        for field in core.FIELDS:
            cleaned[field], found = core.clean_list(record, field)
            primary_candidates += len(record[field]) if isinstance(record.get(field), list) else 0
            issues.extend({'line': line, 'name': name, **issue} for issue in found)
        conflicts = {value[0] for value in cleaned['do_eat']} & {value[0] for value in cleaned['not_eat']}
        for field, (kind, relation_type) in core.FIELDS.items():
            for value, index, raw_value in cleaned[field]:
                if field in {'do_eat', 'not_eat'} and value in conflicts:
                    issues.append({'line': line, 'name': name, 'field': field, 'index': index, 'raw_value': raw_value,
                                   'reason': 'contradictory_food_both_directions_omitted'})
                    continue
                source = proof(line, record, field, raw_value, index)
                end = node(kind, value, source)
                edge(disease, end, relation_type, source)
        companions, found = core.clean_list(record, 'acompany')
        issues.extend({'line': line, 'name': name, **issue} for issue in found)
        for value, index, raw_value in companions:
            if value == name or value not in selected_names:
                issues.append({'line': line, 'name': name, 'field': 'acompany', 'index': index, 'raw_value': raw_value,
                               'reason': 'self_reference' if value == name else 'disease_not_in_source_dataset'})
                continue
            edge(disease, core.identifier('Disease', value), 'acompany_with', proof(line, record, 'acompany', raw_value, index))
        for index, value in enumerate(record.get('drug_detail', [])):
            issues.append({'line': line, 'name': name, 'field': 'drug_detail', 'index': index, 'raw_value': value,
                           'reason': 'unverified_product_details_not_in_graph'})

    nodes = sorted(entities.values(), key=lambda item: (item['type'], item['name']))
    edges = sorted(relations.values(), key=lambda item: (item['type'], item['start_id'], item['end_id']))
    endpoint_keys = {e[k] for e in edges for k in ('start_id', 'end_id')}
    validation = {'missing_endpoints': len(endpoint_keys - set(entities)), 'isolated_nodes': len(set(entities) - endpoint_keys),
                  'empty_names': sum(not n['name'] for n in nodes),
                  'duplicate_entities': len(nodes) - len({(n['type'], n['name']) for n in nodes}),
                  'duplicate_relations': len(edges) - len({(e['type'], e['start_id'], e['end_id']) for e in edges}),
                  'missing_evidence': sum(not item['sources'] for item in nodes + edges),
                  'producer_nodes': sum(n['type'] == 'Producer' for n in nodes),
                  'drugs_of_relations': sum(e['type'] == 'drugs_of' for e in edges)}
    if any(validation.values()):
        raise ValueError('Full dataset validation failed: ' + str(validation))
    report = {'dataset_version': 'full_v1', 'dataset_id': 'medical_full_v1', 'source_file_sha256': hashes,
              'source': {'physical_lines': line_count, 'parsed_records': len(rows), 'parse_errors': parse_errors,
                         'unique_original_names': len({record.get('name') for _, record in rows})},
              'diseases': len(selected), 'excluded_records': len(excluded), 'duplicate_decisions': duplicates,
              'graph': {'entities': len(nodes), 'relations': len(edges), 'primary_list_items_before_filter': primary_candidates,
                        'entities_by_type': dict(sorted(Counter(n['type'] for n in nodes).items())),
                        'relations_by_type': dict(sorted(Counter(e['type'] for e in edges).items())),
                        'skipped_items': len(issues), 'skip_reasons': dict(sorted(Counter(i['reason'] for i in issues).items())),
                        'validation': validation},
              'medical_review_status': 'not_verified', 'manual_review_status': 'pending', 'database_access': 'none'}
    return {'entities.json': nodes, 'relations.json': edges, 'source_records.json': raw_samples,
            'excluded_records.json': excluded, 'skipped_items.json': issues, 'statistics.json': report}, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-existing', action='store_true')
    args = parser.parse_args()
    if not args.verify_existing and OUTPUT.exists():
        parser.error('full_v1 already exists; refusing overwrite')
    package, report = make_package()
    if not args.verify_existing:
        OUTPUT.mkdir(parents=True)
    for name, value in package.items():
        content = core.encode(value)
        if args.verify_existing:
            if (OUTPUT / name).read_bytes() != content:
                raise ValueError('Repeat generation differs: ' + name)
        else:
            with (OUTPUT / name).open('xb') as stream:
                stream.write(content)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
