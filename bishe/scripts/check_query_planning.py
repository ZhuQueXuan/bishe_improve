"""Local planner stubs plus real read-only graph tools; no model accuracy claim."""
import json
import os
import sys
from pathlib import Path
from build_curated_data import PROJECT, SOURCE, digest, encode

sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'knowledge_graph_backend.settings')
import django
django.setup()
from api.graph_retrieval import retrieve_context, graph_statistics, TEXT_FIELDS
from api.fulltext_retrieval import retrieve_phrase
from api.query_planning import planned_retrieval


class LocalPlanner:
    def __init__(self, names, fields, keywords=None):
        self.plan = {'medical': True, 'diseases': names, 'fields': fields, 'keywords': keywords or []}

    def plan_query(self, system, payload):
        assert set(json.loads(payload)) == {'question'}
        return json.dumps(self.plan)


def main():
    output = PROJECT / 'data/query_planning_checks_20261004.json'
    if output.exists():
        raise ValueError('Do not overwrite formal report')
    raw = {line:json.loads(text) for line,text in enumerate(SOURCE.read_text(encoding='utf-8').splitlines(),1)}
    before = graph_statistics()
    source_hash = digest(SOURCE)
    cases = json.loads((PROJECT / 'data/retrieval_baseline_v2.json').read_text(encoding='utf-8'))['cases']
    results, checked = [], 0
    def check(result):
        nonlocal checked
        for fact in result['facts']:
            proof = fact['sources'][0]
            record = raw[proof['line']]
            value = record[proof['field']]
            if 'index' in proof:
                value = value[proof['index']]
            if 'start' in proof:
                value = value[proof['start']:proof['end']]
            elif fact['field'] in TEXT_FIELDS:
                value = value[:1800]
            else:
                value = value.strip()
            assert fact['value'] == value and record['name'].strip() == fact['disease']
            assert record['_id'] == proof['record_id'] and proof['file_sha256'] == source_hash
            assert fact['medical_review_status'] == 'not_verified'
            checked += 1
    for case in cases:
        if case['category'] != 'paraphrase':
            continue
        baseline = retrieve_context(case['query'])
        result = planned_retrieval(case['query'], [], baseline,
                                  LocalPlanner(case['diseases'], case['fields']), retrieve_context, retrieve_phrase)
        check(result)
        assert result['status'] == 'found'
        assert result['diseases'] == case['diseases']
        assert {f['field'] for f in result['facts']} == set(case['fields'])
        results.append({'id':case['id'], 'query':case['query'], 'status':'passed',
                        'planner':'local_fixed_stub', 'retrieval':result})
    history = [{'role':'user','content':'糖尿病是什么？'}]
    baseline = retrieve_context('它怎么防范？', history)
    result = planned_retrieval('它怎么防范？', history, baseline, LocalPlanner([],['prevent']), retrieve_context, retrieve_phrase)
    check(result)
    assert result['diseases'] == ['糖尿病'] and {f['field'] for f in result['facts']} == {'prevent'}
    results.append({'id':'local_history', 'status':'passed', 'retrieval':result})
    baseline = retrieve_context('控制血糖的激素不够的资料')
    result = planned_retrieval('控制血糖的激素不够的资料', [], baseline,
                              LocalPlanner([],['desc','cause'],['胰岛素分泌不足']), retrieve_context, retrieve_phrase)
    check(result)
    assert result['status'] == 'found' and result['resolution'] == 'text_candidates_not_diagnosis'
    results.append({'id':'text_candidate', 'status':'passed', 'retrieval':result})
    assert graph_statistics() == before and digest(SOURCE) == source_hash
    report = {'status':'passed', 'cases':results, 'source_facts_verified':checked,
              'source_sha256':source_hash, 'graph':before, 'database_writes':0, 'external_model_calls':0,
              'limitations':['Planner is mocked: this validates plumbing and evidence, not DeepSeek semantic accuracy.',
                             'Not a 50-case improvement claim; no real model acceptance was run.']}
    output.write_bytes(encode(report))
    print(json.dumps({'status':'passed','cases':len(results),'source_facts_verified':checked,'external_model_calls':0}))


if __name__ == '__main__':
    main()
