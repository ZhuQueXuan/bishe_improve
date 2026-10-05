"""One approved P01/P02/P05/P07 trial. Default uses HTTP mocks, never live.

--execute-authorized is the explicit paid run (16 calls, 1 RMB budget).
"""
import argparse,json,sys,time
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1];WORKSPACE=PROJECT.parent
sys.path.insert(0,str(PROJECT/'knowledge_graph_backend'))
from api.planning_runtime import PlanningHarness,fingerprint
from api.planning_tools import SourceCatalog,SnapshotTools,sha_file
from api.planning_policies import RulePolicy,next_action
from api.deepseek_planner import MockPlannerTransport
from api.authorized_planner import AuthorizedTransport,AuthorizedPlanner,BatchLedger,TASKS
from evaluate_planning_runtime import FixtureTools,score


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-authorized',action='store_true')
    parser.add_argument('--report',required=True,type=Path)
    args=parser.parse_args();output=args.report.resolve()
    if not output.is_relative_to(WORKSPACE/'temp') or output.exists():parser.error('New report inside temp required')
    bundle=PROJECT/'data/planning_challenge_v1';manifest=json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
    for relative,sha in manifest['hashes'].items():assert sha_file(WORKSPACE/relative)==sha
    tasks=[t for t in json.loads((bundle/'tasks.json').read_text(encoding='utf-8'))['tasks'] if t['id'] in TASKS]
    protected=[PROJECT/'data/medical.json',PROJECT/'knowledge_graph_backend/db.sqlite3']
    before={str(p.relative_to(WORKSPACE)):sha_file(p) for p in protected}
    verifier=SourceCatalog(PROJECT/'data',manifest['hashes']['bishe/data/medical.json'])
    tools=SnapshotTools(verifier,[t['seed']['id'] for t in tasks]);results=[]
    # Run the zero-model comparator before constructing any live SDK client.
    def run_case(task,policy,fixtures,external=False):
        controller=PlanningHarness(task,fixtures,verifier,fingerprint(manifest),WORKSPACE/'temp',allow_external=external)
        records=[];start=time.monotonic();resumed=False
        if task['scenario']=='resume_after_relations':
            controller.step(policy)
            if controller.state['phase']=='active':
                checkpoint=output.with_name(output.stem+'_'+('live' if external else 'model' if hasattr(policy,'records') else 'rule')+'_'+task['id']+'_checkpoint.json')
                controller.checkpoint(checkpoint)
                controller=PlanningHarness.restore(checkpoint,task,fixtures,verifier,fingerprint(manifest),WORKSPACE/'temp',allow_external=external)
                resumed=True
        for _ in range(12):
            if controller.state['phase']!='active':break
            if external and ledger.data['halted']:controller.block('batch_halted');break
            controller.step(policy)
        if controller.state['phase']=='active':controller.block('step_budget')
        return {'task_id':task['id'],'resumed':resumed,'tool_calls':fixtures.calls,'result':controller.result(),
                'elapsed_ms':int(1000*(time.monotonic()-start))}
    for task in tasks:
        item=run_case(task,RulePolicy(),FixtureTools(tools,task['scenario']));item['policy']='rule';results.append(item)
    ledger=BatchLedger(output.with_name(output.stem+'_ledger.json'),WORKSPACE/'temp')
    mock=None
    if args.execute_authorized:
        import os
        from dotenv import load_dotenv
        from openai import OpenAI
        from urllib.parse import urlsplit
        load_dotenv(PROJECT/'knowledge_graph_backend/.env')
        base=os.getenv('DEEPSEEK_API_BASE_URL','https://api.deepseek.com');parts=urlsplit(base)
        if parts.scheme!='https' or parts.hostname!='api.deepseek.com' or parts.path.rstrip('/') not in ('','/v1'):
            raise ValueError('Only official DeepSeek endpoint permitted')
        key=os.getenv('DEEPSEEK_API_KEY')
        if not key:raise ValueError('Configured DeepSeek key missing')
        client=OpenAI(api_key=key,base_url=base,max_retries=0)
    else:
        import httpx2 as httpx
        def handler(request):
            body=json.loads(request.content);context=json.loads(body['messages'][1]['content'])['task_state']
            return httpx.Response(200,json={'id':'mock','object':'chat.completion','created':0,'model':'deepseek-flash',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(next_action(context),ensure_ascii=False)}}],
                'usage':{'prompt_tokens':300,'completion_tokens':50,'total_tokens':350}})
        mock=MockPlannerTransport(handler);client=mock._client
    transport=AuthorizedTransport(client,ledger)
    if not args.execute_authorized:transport.label='authorized_ledger_sdk_mock'
    try:
        for task in tasks:
            if ledger.data['halted']:break
            transport.task_id=task['id'];policy=AuthorizedPlanner(transport)
            if not args.execute_authorized:policy.local_only=True
            item=run_case(task,policy,FixtureTools(tools,task['scenario']),args.execute_authorized)
            item.update(policy='deepseek_dynamic' if args.execute_authorized else 'authorized_sdk_mock',planner_records=policy.records)
            results.append(item)
            print(json.dumps({'task':task['id'],'status':item['result']['status'],'reads':item['result']['counts']['reads'],
                              'decisions':item['result']['counts']['decisions'],'batch_requests':len(ledger.data['calls'])}),flush=True)
    finally:client.close()
    score(results,bundle)
    assert all(sha_file(p)==before[str(p.relative_to(WORKSPACE))] for p in protected)
    summary={name:{'passed':sum(x['score']['passed'] for x in results if x['policy']==name),
                   'total':sum(x['policy']==name for x in results),
                   'read_attempts':sum(x['result']['counts']['reads'] for x in results if x['policy']==name)}
             for name in {x['policy'] for x in results}}
    from decimal import Decimal
    total=sum(Decimal(x.get('estimated_rmb',x['reserved_rmb'])) for x in ledger.data['calls'])
    report={'status':'trial_recorded','mode':'authorized_live' if args.execute_authorized else 'budgeted_sdk_mock',
            'cases':results,'summary':summary,'ledger':ledger.data,'requests':len(ledger.data['calls']),
            'external_request_attempts':len(ledger.data['calls']) if args.execute_authorized else 0,
            'estimated_peak_rmb':str(total),'cost_is_conservative_estimate_not_invoice':True,
            'protected_hashes_unchanged':before,'database_access':False,'answers_generated':0,
            'limitations':['Only four development tasks, one run each; no statistical/generalization claim.',
                           'Cost uses official peak USD rates and conservative FX coefficient 8, not provider invoice.']}
    with output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps({'summary':summary,'requests':report['requests'],'estimated_peak_rmb':str(total),'halted':ledger.data['halted']},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
