"""Offline by default; explicitly check source-backed Agent runs or replay saved tool returns.

Model plans are fixed local stubs. No external model calls or account database use.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parent
sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))


def report_path(path):
    output = path.resolve()
    if not output.is_relative_to(WORKSPACE / 'temp') or output.exists():
        raise ValueError('Report must be a new file inside project temp')
    return output


def cases():
    baseline = json.loads((PROJECT / 'data/retrieval_baseline_v2.json').read_text(encoding='utf-8'))['cases']
    result = [{'id': c['id'], 'query': c['query'], 'history': [],
               'plan': {'medical': True, 'diseases': c['diseases'], 'fields': c['fields'], 'keywords': []},
               'expected_diseases': c['diseases'], 'expected_fields': c['fields']}
              for c in baseline if c['category'] == 'paraphrase']
    result += [
        {'id':'local_history','query':'它怎么防范？','history':[{'role':'user','content':'糖尿病是什么？'}],
         'plan':{'medical':True,'diseases':[],'fields':['prevent'],'keywords':[]},
         'expected_diseases':['糖尿病'],'expected_fields':['prevent']},
        {'id':'text_candidate','query':'控制血糖的激素不够的资料','history':[],
         'plan':{'medical':True,'diseases':[],'fields':['desc','cause'],'keywords':['胰岛素分泌不足']},
         'expected_fields':['desc','cause']},
        {'id':'multi_field_observation','query':'糖尿病概况和诱因','history':[],
         'plan':{'medical':True,'diseases':['糖尿病'],'fields':['desc','cause'],'keywords':[]},
         'expected_diseases':['糖尿病'],'expected_fields':['desc','cause']},
    ]
    return result


def model_for(case):
    model = Mock()
    def local_plan(system, payload, timeout=15):
        assert json.loads(payload) == {'question': case['query']}
        return json.dumps(case['plan'], ensure_ascii=False)
    model.plan_query.side_effect = local_plan
    return model


def verify_source(result, records, source_hash):
    count = 0
    from api.graph_retrieval import TEXT_FIELDS
    for fact in result['facts']:
        source = fact['sources'][0]
        raw = records[source['line']]
        value = raw[source['field']]
        if 'index' in source: value = value[source['index']]
        if 'start' in source: value = value[source['start']:source['end']]
        elif fact['field'] in TEXT_FIELDS: value = value[:1800]
        else: value = value.strip()
        assert value == fact['value'] and source['file_sha256'] == source_hash
        assert raw['_id'] == source['record_id'] and raw['name'].strip() == fact['disease']
        count += 1
    return count


def signature(result):
    return {'status':result['status'], 'facts':result['facts'],
            'stop_reason':result['agent']['stop_reason'], 'counts':result['agent']['counts'],
            'steps':[{k:v for k,v in step.items() if k not in {'timeout_seconds','elapsed_ms'}}
                     for step in result['agent']['steps']]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--read-only',action='store_true')
    mode.add_argument('--replay',type=Path)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if not args.read_only and not args.replay:
        print(json.dumps({'mode':'offline_preview','cases':len(cases()),'external_model_calls':0,'database_access':False}))
        return
    if args.report is None: parser.error('--report is required')
    output=report_path(args.report)
    from api.agent_loop import BoundedAgent
    if args.replay:
        source=args.replay.resolve()
        if not source.is_relative_to(WORKSPACE/'temp'):
            raise ValueError('Replay input must be inside project temp')
        report=json.loads(source.read_text(encoding='utf-8'))
        checked=[]
        for item in report['cases']:
            recorded=iter(item['tool_returns'])
            consumed=[]
            def replay(tool, inputs):
                event=next(recorded)
                assert event['tool']==tool and event['input']==inputs
                consumed.append(event)
                return event['result']
            graph=lambda q,h,f=None,timeout=30: replay('graph',{'query':q,'fields':f})
            text=lambda p,limit=5,timeout=30: replay('fulltext',{'phrase':p})
            case=item['case']
            model=model_for(case)
            result=BoundedAgent(graph,text,lambda:model).retrieve(case['query'],case['history'])
            assert list(recorded)==[]
            assert signature(result)==signature(item['retrieval'])
            checked.append({'id':case['id'],'status':'replayed_same_decisions_evidence_and_stop'})
        result={'status':'passed','mode':'offline_replay','cases':checked,'external_model_calls':0,'database_access':False}
    else:
        os.environ.setdefault('DJANGO_SETTINGS_MODULE','knowledge_graph_backend.settings')
        import django
        django.setup()
        from api.graph_retrieval import retrieve_context, graph_statistics
        from api.fulltext_retrieval import retrieve_phrase
        source=PROJECT/'data/medical.json'
        source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
        records={i:json.loads(s) for i,s in enumerate(source.read_text(encoding='utf-8').splitlines(),1)}
        before=graph_statistics()
        results=[]; verified=0
        for case in cases():
            returns=[]
            def graph(q,h,f=None,timeout=30):
                result=retrieve_context(q,h,f,timeout=timeout)
                returns.append({'tool':'graph','input':{'query':q,'fields':f},'result':result})
                return result
            def text(p,limit=5,timeout=30):
                result=retrieve_phrase(p,limit=limit,timeout=timeout)
                returns.append({'tool':'fulltext','input':{'phrase':p},'result':result})
                return result
            model=model_for(case)
            result=BoundedAgent(graph,text,lambda:model).retrieve(case['query'],case['history'])
            assert result['status']=='found',case['id']
            if 'expected_diseases' in case: assert result['diseases']==case['expected_diseases'],case['id']
            assert {f['field'] for f in result['facts']}==set(case['expected_fields']),case['id']
            assert result['agent']['counts']['retrieval'] <= 3
            model.knowledge_qa.assert_not_called()
            verified+=verify_source(result,records,source_hash)
            results.append({'case':case,'retrieval':result,'tool_returns':returns,'model':'fixed_local_stub'})
        assert graph_statistics()==before
        assert hashlib.sha256(source.read_bytes()).hexdigest()==source_hash
        result={'status':'passed','mode':'read_only_fixed_plans','cases':results,'source_facts_verified':verified,
                'graph':before,'source_sha256':source_hash,'external_model_calls':0,'database_writes':0,
                'limitations':['Fixed plans validate wiring, not live model quality.','No final answers generated.']}
    with output.open('x',encoding='utf-8') as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps({'status':result['status'],'mode':result['mode'],'cases':len(result['cases']),
                      'external_model_calls':0}))


if __name__=='__main__': main()
