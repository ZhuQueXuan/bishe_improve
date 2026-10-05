"""Explicit read-only checks against the configured full graph. No external model calls."""
import argparse
import json
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--read-only', action='store_true', help='Explicitly enable local database reads')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if not args.read_only:
        parser.error('Use --read-only to explicitly check the configured graph')
    destination = args.report.resolve() if args.report else None
    if destination and (not destination.is_relative_to(PROJECT.parent / 'temp') or destination.exists()):
        parser.error('Report must be a new file under project temp')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'knowledge_graph_backend.settings')
    import django
    django.setup()
    from api.graph_retrieval import retrieve_context, graph_statistics, TEXT_FIELDS
    raw = {}
    with (PROJECT / 'data/medical.json').open(encoding='utf-8') as stream:
        for line, text in enumerate(stream, 1):
            raw[line] = json.loads(text)
    cases = json.loads((PROJECT / 'data/retrieval_cases_v1.json').read_text(encoding='utf-8'))
    before = graph_statistics()
    results = []
    for case in cases:
        result = retrieve_context(case['query'], case.get('history'))
        checks = [result['status'] == case.get('status', 'found')]
        if 'disease' in case:
            checks.extend([result['diseases'] == [case['disease']],
                           {fact['field'] for fact in result['facts']} == set(case['fields'])])
        if 'resolution' in case:
            checks.append(result['resolution'] == case['resolution'])
        for fact in result['facts']:
            proof = fact['sources'][0]
            original = raw[proof['line']]
            value = original[proof['field']]
            if 'index' in proof:
                value = value[proof['index']]
            expected = value[:1800] if fact['field'] in TEXT_FIELDS else value.strip()
            checks.extend([original['name'].strip() == fact['disease'],
                           original['_id'] == proof['record_id'], fact['value'] == expected,
                           fact['medical_review_status'] == 'not_verified'])
        if not all(checks):
            raise ValueError('Retrieval check failed: ' + case['id'])
        results.append({'id': case['id'], 'status': 'passed', 'facts': len(result['facts'])})
    after = graph_statistics()
    if before != after:
        raise ValueError('Graph statistics changed during read-only checks')
    report = {'status': 'passed', 'cases': results, 'graph': after,
              'external_model_calls': 0, 'database_writes': 0}
    if destination:
        with destination.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    print(json.dumps({'status': 'passed', 'cases': len(results), 'external_model_calls': 0}))


if __name__ == '__main__':
    main()
