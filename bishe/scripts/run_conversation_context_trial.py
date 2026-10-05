"""8 synthetic sessions x 2 contexts, max16/1RMB. Default SDK MOCK only."""
import argparse,json,sys
from decimal import Decimal
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1];WORKSPACE=PROJECT.parent
sys.path.insert(0,str(PROJECT/'knowledge_graph_backend'))
from api.conversation_context import SessionMemory,build_context,window_context
from api.conversation_reader import ConversationReader,ContextBatchLedger,SYSTEM,POLICIES
from api.authorized_planner import AuthorizedTransport
from api.deepseek_planner import MockPlannerTransport
from api.planning_runtime import BoundaryError
from evaluate_conversation_context import sha,mock_read


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-authorized',action='store_true')
    parser.add_argument('--report',required=True,type=Path)
    args=parser.parse_args();out=args.report.resolve()
    if not out.is_relative_to(WORKSPACE/'temp') or out.exists():parser.error('New temp report required')
    bundle=PROJECT/'data/conversation_context_v1'
    manifest=json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
    for n,h in manifest['hashes'].items():assert sha(bundle/n)==h
    tasks=json.loads((bundle/'tasks.json').read_text(encoding='utf-8'));catalog=tasks['catalog'];rows=[]
    protected=[PROJECT/'data/medical.json',PROJECT/'knowledge_graph_backend/db.sqlite3']
    before={str(p.relative_to(WORKSPACE)):sha(p) for p in protected}
    # Count common system content inside the total budget for BOTH arms.
    for case in tasks['cases']:
        budget=case['budget_bytes']-len(SYSTEM.encode('utf-8'))
        if budget<128:raise BoundaryError('preflight_system_budget')
        memory=SessionMemory(case['session_id'],catalog)
        for message in case['messages']:memory.ingest(message)
        current=[m for m in case['messages'] if m['session_id']==case['session_id']][-1]['id']
        packets={'state_context':build_context(memory,current,budget),
                 'window10_surrogate':window_context(case['messages'],case['session_id'],catalog,budget)[0]}
        for policy in POLICIES:
            packet=packets[policy]
            if packet['payload'] is None:raise BoundaryError('preflight_mandatory_context')
            rows.append({'case_id':case['id'],'split':case['split'],'policy':policy,
                         'total_budget_bytes':case['budget_bytes'],'context':packet,
                         'prediction':None,'record':{'status':'not_run'}})
    # Every payload is built without loading the oracle; stable paired order.
    ledger=ContextBatchLedger(out.with_name(out.stem+'_ledger.json'),WORKSPACE/'temp')
    if args.execute_authorized:
        import os
        from urllib.parse import urlsplit
        from dotenv import load_dotenv
        from openai import OpenAI
        load_dotenv(PROJECT/'knowledge_graph_backend/.env')
        base=os.getenv('DEEPSEEK_API_BASE_URL','https://api.deepseek.com');parts=urlsplit(base)
        if parts.scheme!='https' or parts.hostname!='api.deepseek.com' or parts.path.rstrip('/') not in ('','/v1'):
            raise BoundaryError('official_endpoint_required')
        key=os.getenv('DEEPSEEK_API_KEY')
        if not key:raise BoundaryError('configured_key_missing')
        client=OpenAI(api_key=key,base_url=base,max_retries=0)
    else:
        import httpx2 as httpx
        def handler(req):
            body=json.loads(req.content)
            prediction=mock_read({'payload':body['messages'][1]['content']},catalog,tasks['cases'][0]['session_id'])
            return httpx.Response(200,json={'id':'mock','created':0,'object':'chat.completion','model':'deepseek-flash',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(prediction,ensure_ascii=False)}}],
                'usage':{'prompt_tokens':300,'completion_tokens':50,'total_tokens':350}})
        mock=MockPlannerTransport(handler);client=mock._client
    transport=AuthorizedTransport(client,ledger)
    if not args.execute_authorized:transport.label='context_ledger_sdk_mock'
    reader=ConversationReader(transport,catalog)
    partial=out.with_name(out.stem+'_partial.json')
    with partial.open('x',encoding='utf-8') as f:json.dump({'cases':rows},f,ensure_ascii=False,indent=2)
    try:
        for row in rows:
            if ledger.data['halted']:
                row['record']={'status':'batch_halted'};continue
            transport.task_id=f"{row['case_id']}:{row['policy']}"
            count=len(reader.records)
            try:row['prediction']=reader.read(row['context'],row['total_budget_bytes'])
            except BoundaryError as error:row['record']={'status':str(error)}
            if len(reader.records)>count:row['record']=reader.records[-1]
            with partial.open('w',encoding='utf-8') as f:json.dump({'cases':rows},f,ensure_ascii=False,indent=2)
            print(json.dumps({'case':row['case_id'],'policy':row['policy'],'status':row['record']['status'],
                              'requests':len(ledger.data['calls'])}),flush=True)
    finally:client.close()
    oracle=json.loads((bundle/'oracle.json').read_text(encoding='utf-8'))
    for row in rows:
        row['expected']=oracle[row['case_id']];row['passed']=row['prediction']==row['expected']
    summary={p:{'passed':sum(x['passed'] for x in rows if x['policy']==p),'total':8,
                'prompt_tokens':sum(x['record'].get('usage',{}).get('prompt_tokens',0) for x in rows if x['policy']==p and x['record'].get('usage'))}
             for p in POLICIES}
    assert before=={str(p.relative_to(WORKSPACE)):sha(p) for p in protected}
    cost=sum(Decimal(x.get('estimated_rmb',x['reserved_rmb'])) for x in ledger.data['calls'])
    report={'mode':'authorized_live' if args.execute_authorized else 'sdk_mock','summary':summary,'cases':rows,
            'requests':len(ledger.data['calls']),'ledger':ledger.data,'estimated_peak_rmb':str(cost),
            'cost_not_invoice':True,'external_calls':len(ledger.data['calls']) if args.execute_authorized else 0,
            'database_access':False,'protected_hashes_unchanged':before,
            'limitations':['Synthetic explicit phrases, single paired run, not blind or statistically significant.',
                           'Window arm is budget-matched proxy, not production API.',
                           'Measures state reading, not clinical/answer/retrieval quality.']}
    with out.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps({'summary':summary,'requests':report['requests'],'estimated_peak_rmb':str(cost),'halted':ledger.data['halted']}),flush=True)


if __name__=='__main__':main()
