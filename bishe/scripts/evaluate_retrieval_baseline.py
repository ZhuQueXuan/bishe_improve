"""Measure current retrieval against fixed source-grounded expectations; read-only, no LLM."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parent
sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))
import build_curated_data as builder

FIELD_SOURCE = {kind: field for field, (_, kind) in builder.FIELDS.items()}
FIELD_SOURCE['acompany_with'] = 'acompany'
TEXT_FIELDS = {'desc', 'cause', 'prevent'}


def expected_for(case, records):
    if 'oracle' in case:
        symptoms = set(case['oracle']['symptoms_all'])
        diseases = sorted(name for name, item in records.items()
                          if symptoms <= {v[0] for v in builder.clean_list(item['raw_record'], 'symptom')[0]})
        if not diseases:
            raise ValueError('Empty source oracle: ' + case['id'])
        evidence = {(name, 'has_symptom', symptom) for name in diseases for symptom in symptoms}
    else:
        diseases = sorted(case.get('diseases', []))
        evidence = set()
        for name in diseases:
            record = records[name]['raw_record']
            for field in case.get('fields', []):
                if field in TEXT_FIELDS:
                    if record.get(field):
                        evidence.add((name, field, record[field][:1800]))
                else:
                    evidence.update((name, field, value[0]) for value in builder.clean_list(record, FIELD_SOURCE[field])[0])
    if case.get('status', 'found') == 'found':
        if not diseases or not case.get('fields'):
            raise ValueError('Found expectation must have source-backed diseases and fields')
        for disease in diseases:
            for field in case['fields']:
                if not any(item[:2] == (disease, field) for item in evidence):
                    raise ValueError('Expectation lacks source evidence: ' + case['id'])
    return {'status': case.get('status', 'found'), 'diseases': diseases,
            'fields': sorted(case.get('fields', [])), 'evidence': evidence}


def check_sources(result, originals, source_hash):
    errors = []
    for fact in result.get('facts', []):
        for proof in fact.get('sources', []):
            raw = originals.get(proof.get('line'))
            if not raw:
                errors.append('missing_source_line')
                continue
            field = proof.get('field')
            value = raw.get(field)
            if 'index' in proof:
                index = proof['index']
                value = value[index] if isinstance(value, list) and isinstance(index, int) and 0 <= index < len(value) else None
            expected = value[:1800] if field in TEXT_FIELDS and isinstance(value, str) else value.strip() if isinstance(value, str) else None
            if (proof.get('file') != 'bishe/data/medical.json' or proof.get('file_sha256') != source_hash
                    or raw.get('_id') != proof.get('record_id') or raw.get('name', '').strip() != fact.get('disease')
                    or proof.get('disease', '').strip() != fact.get('disease')
                    or field != FIELD_SOURCE.get(fact.get('field'), fact.get('field'))
                    or expected != fact.get('value') or fact.get('medical_review_status') != 'not_verified'):
                errors.append('source_content_mismatch')
        if not fact.get('sources'):
            errors.append('missing_source')
    return sorted(set(errors))


def score_case(case, result, expected, source_errors):
    actual_diseases = sorted(set(result.get('diseases', [])))
    actual_fields = sorted({fact['field'] for fact in result.get('facts', [])})
    actual_evidence = {(fact['disease'], fact['field'], fact['value']) for fact in result.get('facts', [])}
    required = expected['evidence']
    relevant = required & actual_evidence
    missing = required - actual_evidence
    failures = []
    if result['status'] != expected['status']:
        failures.append('status_mismatch')
    if expected['status'] == 'found':
        if actual_diseases != expected['diseases']:
            failures.append('disease_set_mismatch')
        if actual_fields != expected['fields']:
            failures.append('field_set_mismatch')
        for disease in expected['diseases']:
            for field in expected['fields']:
                if not any(item[:2] == (disease, field) for item in relevant):
                    failures.append('missing_disease_field')
                    break
        if case.get('complete_evidence') and missing:
            failures.append('incomplete_evidence')
        if case.get('resolution') and case['resolution'] != result.get('resolution'):
            failures.append('resolution_mismatch')
    elif result.get('facts'):
        failures.append('unexpected_evidence')
    if source_errors:
        failures.append('invalid_provenance')
    return {'passed': not failures, 'failures': sorted(set(failures)),
            'actual_status': result['status'], 'actual_diseases': actual_diseases, 'actual_fields': actual_fields,
            'expected_status': expected['status'], 'expected_diseases': expected['diseases'], 'expected_fields': expected['fields'],
            'required_evidence_count': len(required), 'returned_evidence_count': len(actual_evidence),
            'relevant_evidence_count': len(relevant), 'missing_evidence_count': len(missing),
            'evidence_recall': round(len(relevant) / len(required), 4) if required else None,
            'evidence_precision': round(len(relevant) / len(actual_evidence), 4) if actual_evidence else None,
            'source_errors': source_errors,
            'missing_examples': [dict(disease=d, field=f, value=v) for d, f, v in sorted(missing)[:6]],
            'unexpected_examples': [dict(disease=d, field=f, value=v) for d, f, v in sorted(actual_evidence - required)[:6]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--read-only', action='store_true')
    parser.add_argument('--cases', type=Path, default=PROJECT / 'data/retrieval_baseline_v2.json')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if not args.read_only:
        parser.error('Explicit --read-only required')
    destination = args.report.resolve()
    if not destination.is_relative_to(WORKSPACE / 'temp') or destination.exists():
        parser.error('Use a new report path under project temp')
    source_hash = builder.digest(builder.SOURCE)
    originals = dict(builder.load_records(builder.SOURCE)[0])
    selected_path = PROJECT / 'data/full_v1/source_records.json'
    selected = json.loads(selected_path.read_text(encoding='utf-8'))
    for item in selected:
        proof = item['source']
        if proof['file_sha256'] != source_hash or item['raw_record'] != originals.get(proof['line']):
            raise ValueError('Selected source record differs from original data')
    records = {item['raw_record']['name'].strip(): item for item in selected}
    suite = json.loads(args.cases.read_text(encoding='utf-8'))
    expectations = [expected_for(case, records) for case in suite['cases']]
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'knowledge_graph_backend.settings')
    import django
    django.setup()
    from api import graph_retrieval as graph
    before = graph.graph_statistics()
    results = []
    for case, expected in zip(suite['cases'], expectations):
        started = time.perf_counter()
        result = graph.retrieve_context(case['query'], case.get('history'))
        item = {'id': case['id'], 'category': case['category'], 'query': case['query'],
                **score_case(case, result, expected, check_sources(result, originals, source_hash)),
                'elapsed_ms': round((time.perf_counter() - started) * 1000, 2),
                'retrieval_truncated_flag': result.get('truncated', False),
                'facts': result.get('facts', [])}
        results.append(item)
    after = graph.graph_statistics()
    if before != after or builder.digest(builder.SOURCE) != source_hash:
        raise ValueError('Graph statistics or original data changed during evaluation')
    by_category = {}
    for category in suite['categories']:
        group = [r for r in results if r['category'] == category]
        required = sum(r['required_evidence_count'] for r in group)
        relevant = sum(r['relevant_evidence_count'] for r in group)
        returned = sum(r['returned_evidence_count'] for r in group)
        by_category[category] = {'label': suite['categories'][category], 'total': len(group),
            'passed': sum(r['passed'] for r in group),
            'micro_evidence_recall': round(relevant / required, 4) if required else None,
            'micro_evidence_precision': round(relevant / returned, 4) if returned else None}
    report = {'evaluation_status': 'completed', 'suite_version': suite['version'],
        'cases_sha256': builder.digest(args.cases), 'retrieval_code_sha256': builder.digest(Path(graph.__file__)),
        'medical_source_sha256': source_hash, 'dataset_id': graph.DATASET,
        'selected_source_sha256': builder.digest(selected_path),
        'external_model_calls': 0, 'database_writes': 0, 'graph_before': before, 'graph_after': after,
        'case_count': len(results), 'passed': sum(r['passed'] for r in results),
        'failed': sum(not r['passed'] for r in results),
        'facts_checked': sum(len(r['facts']) for r in results),
        'source_error_cases': sum(bool(r['source_errors']) for r in results),
        'categories': by_category, 'results': results,
        'interpretation': 'Fixed engineering task set; failures measure unsupported requested retrieval behavior. Not a population benchmark or medical accuracy metric. Zero evidence is not counted as validated provenance.'}
    with destination.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({k: report[k] for k in ('evaluation_status', 'case_count', 'passed', 'failed', 'source_error_cases', 'categories')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
