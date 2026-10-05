"""Local read-only smoke/comparison; no external models and no clinical claims."""
import json
import os
import sys
import time
from pathlib import Path
from build_curated_data import PROJECT, SOURCE, encode, digest

sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'knowledge_graph_backend.settings')
import django
django.setup()
from api.fulltext_retrieval import retrieve_phrase
from api.graph_retrieval import retrieve_context, graph_statistics

PHRASES = [
    ('内分泌代谢性疾病', '糖尿病'), ('胰岛素分泌不足', '糖尿病'),
    ('体循环动脉压增高', '高血压'), ('血压升高是某些疾病', '高血压'),
    ('胃粘膜上皮', '慢性胃炎'), ('胃粘膜的慢性炎症', '慢性胃炎'),
    ('气道反应性增高', '支气管哮喘'), ('反复发作的喘息', '支气管哮喘'),
]


def main():
    output = PROJECT / 'data/fulltext_comparison_v1.json'
    if output.exists():
        raise ValueError('Formal report already exists; do not overwrite')
    raw = {line: json.loads(text) for line, text in enumerate(SOURCE.read_text(encoding='utf-8').splitlines(), 1)}
    before = graph_statistics()
    source_hash = digest(SOURCE)
    checks = 0
    def search(query):
        nonlocal checks
        start = time.perf_counter()
        result = retrieve_phrase(query, limit=10)
        elapsed = round((time.perf_counter() - start) * 1000, 2)
        for fact in result['facts']:
            proof = fact['sources'][0]
            original = raw[proof['line']]
            assert proof['file_sha256'] == source_hash
            assert original['_id'] == proof['record_id']
            assert original['name'].strip() == fact['disease']
            assert original[proof['field']][proof['start']:proof['end']] == fact['value']
            assert query in fact['value'] and fact['medical_review_status'] == 'not_verified'
            checks += 1
        return result, elapsed
    phrase_results = []
    for phrase, disease in PHRASES:
        # Require the fixed target to actually contain this phrase before comparing.
        assert any(r['name'].strip() == disease and any(phrase in r.get(f, '') for f in ['desc', 'cause', 'prevent']) for r in raw.values())
        result, elapsed = search(phrase)
        graph = retrieve_context(phrase)
        phrase_results.append({'phrase': phrase, 'expected_source_disease': disease,
                               'fulltext_target_found': any(f['disease'] == disease for f in result['facts']),
                               'graph_target_found': any(f['disease'] == disease for f in graph['facts']),
                               'fulltext_latency_ms': elapsed, 'retrieval': result})
    cases_path = PROJECT / 'data/retrieval_baseline_v2.json'
    cases = json.loads(cases_path.read_text(encoding='utf-8'))['cases']
    baseline = []
    for case in cases:
        result, elapsed = search(case['query'])
        baseline.append({'id': case['id'], 'query': case['query'], 'category': case['category'],
                         'fulltext_status': result['status'], 'facts': len(result['facts']),
                         'fulltext_latency_ms': elapsed})
    # Literal tools deliberately do not infer history or answer natural questions.
    for phrase in ['不存在的医疗资料XYZ123', '" OR *:*', '厂家品牌商品名XYZ123']:
        result, _ = search(phrase)
        assert not result['facts']
    assert graph_statistics() == before and digest(SOURCE) == source_hash
    report = {'scope': 'standalone_literal_tool_not_chat_hybrid_or_clinical_accuracy',
              'source_sha256': source_hash, 'baseline_cases_sha256': digest(cases_path),
              'phrase_cases': phrase_results, 'existing_50_cases': baseline,
              'summary': {'phrase_cases': len(phrase_results),
                          'fulltext_target_found': sum(r['fulltext_target_found'] for r in phrase_results),
                          'graph_target_found': sum(r['graph_target_found'] for r in phrase_results),
                          'natural_question_literal_hits': sum(r['facts'] > 0 for r in baseline),
                          'source_facts_verified': checks, 'protection_cases': 3,
                          'graph_unchanged': True, 'database_writes': 0, 'external_model_calls': 0},
              'limitations': ['Exact phrases from raw text are a smoke test, not general question accuracy.',
                              'The existing 50 natural questions remain the graph baseline; full-text literal hits are not pass rates.',
                              'No vectors, rank fusion or change to the chat route in this milestone.']}
    output.write_bytes(encode(report))
    print(json.dumps(report['summary']))


if __name__ == '__main__':
    main()
