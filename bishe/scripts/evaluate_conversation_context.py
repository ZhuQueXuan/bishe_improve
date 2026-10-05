"""Offline context availability comparison; never a real model benchmark."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1]
WORKSPACE=PROJECT.parent
sys.path.insert(0,str(PROJECT/'knowledge_graph_backend'))
from api.conversation_context import SessionMemory,build_context,window_context


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def mock_read(packet,catalog,session):
    """Read only serialized input, never hidden memory or oracle."""
    if packet['payload'] is None:return {'status':'context_limit'}
    data=json.loads(packet['payload'])
    if 'state' in data:
        entries=data['state']['entries']
        def value(key):return entries.get(key,{}).get('value')
        objects=value('objects') or []
        return {'status':'ready' if len(objects)==1 else 'clarify','objects':objects,
                'field':value('field'),'format':value('format')}
    state=SessionMemory(session,catalog)
    for m in data['history']:state.ingest(m)
    return state.snapshot()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split',choices=['dev','holdout','all'],default='dev')
    parser.add_argument('--report',required=True,type=Path)
    args=parser.parse_args();out=args.report.resolve()
    if not out.is_relative_to(WORKSPACE/'temp') or out.exists():parser.error('New temp report required')
    bundle=PROJECT/'data/conversation_context_v1'
    manifest=json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
    for name,h in manifest['hashes'].items():assert sha(bundle/name)==h
    tasks=json.loads((bundle/'tasks.json').read_text(encoding='utf-8'))
    protected=[PROJECT/'data/medical.json',PROJECT/'knowledge_graph_backend/db.sqlite3']
    before={str(p.relative_to(WORKSPACE)):sha(p) for p in protected}
    rows=[]
    for case in tasks['cases']:
        if args.split!='all' and case['split']!=args.split:continue
        memory=SessionMemory(case['session_id'],tasks['catalog'])
        for m in case['messages']:memory.ingest(m)
        current=[m for m in case['messages'] if m['session_id']==case['session_id']][-1]['id']
        candidate=build_context(memory,current,case['budget_bytes'])
        window,_=window_context(case['messages'],case['session_id'],tasks['catalog'],case['budget_bytes'])
        for policy,packet in [('window10_surrogate',window),('state_context',candidate)]:
            assert packet['input_bytes']<=case['budget_bytes']
            rows.append({'case_id':case['id'],'split':case['split'],'policy':policy,
                         'budget_bytes':case['budget_bytes'],'context':packet,
                         'prediction':mock_read(packet,tasks['catalog'],case['session_id'])})
    # Expected outcomes are loaded only after both policies produced all inputs.
    oracle=json.loads((bundle/'oracle.json').read_text(encoding='utf-8'))
    for row in rows:
        row['expected']=oracle[row['case_id']]
        row['passed']=row['prediction']==row['expected']
    assert before=={str(p.relative_to(WORKSPACE)):sha(p) for p in protected}
    summary={p:{'passed':sum(x['passed'] for x in rows if x['policy']==p),
                'total':sum(x['policy']==p for x in rows),
                'input_bytes':sum(x['context']['input_bytes'] for x in rows if x['policy']==p)}
             for p in ['window10_surrogate','state_context']}
    result={'status':'offline_recorded','split':args.split,'summary':summary,'cases':rows,
            'external_model_calls':0,'database_access':False,'protected_hashes_unchanged':before,
            'manifest_sha256':sha(bundle/'manifest.json'),
            'limitations':['Synthetic explicit grammar and mock input reader, not real model success.',
                           'Window baseline is budget-matched information surrogate, not production API.',
                           'Reserved cases author-visible at creation, not blind.']}
    with out.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({'split':args.split,'summary':summary,'external_model_calls':0}))


if __name__=='__main__':main()
