"""Actual SDK request contract over local HTTP mocks, source-backed 8-task run."""
import argparse
import json
import sys
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[1]
WORKSPACE=PROJECT.parent
sys.path.insert(0,str(PROJECT/'knowledge_graph_backend'))
from api.deepseek_planner import DeepSeekPlanner,MockPlannerTransport
from api.planning_runtime import PlanningHarness,fingerprint
from api.planning_tools import SourceCatalog,SnapshotTools,sha_file
from api.planning_policies import next_action
from evaluate_planning_runtime import FixtureTools,score


def main():
    import httpx2 as httpx
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,type=Path)
    args=parser.parse_args(); output=args.report.resolve()
    if not output.is_relative_to(WORKSPACE/'temp') or output.exists():parser.error('New report inside temp required')
    bundle=PROJECT/'data/planning_challenge_v1'
    manifest=json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
    for path,sha in manifest['hashes'].items():assert sha_file(WORKSPACE/path)==sha
    protected=[PROJECT/'data/medical.json',PROJECT/'knowledge_graph_backend/db.sqlite3']
    before={str(p.relative_to(WORKSPACE)):sha_file(p) for p in protected}
    tasks=json.loads((bundle/'tasks.json').read_text(encoding='utf-8'))['tasks']
    verifier=SourceCatalog(PROJECT/'data',manifest['hashes']['bishe/data/medical.json'])
    tools=SnapshotTools(verifier,[t['seed']['id'] for t in tasks])
    cases=[]; requests=[]
    def handler(request):
        body=json.loads(request.content)
        assert body['model']=='deepseek-flash' and body['thinking']=={'type':'disabled'}
        assert body['max_tokens']==300 and body['response_format']=={'type':'json_object'}
        assert body['stream'] is False and body['temperature']==0
        requests.append(body)
        state=json.loads(body['messages'][1]['content'])['task_state']
        action=next_action(state)
        return httpx.Response(200,json={'id':'local-mock','object':'chat.completion','created':0,'model':'deepseek-flash',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(action,ensure_ascii=False)}}],
            'usage':{'prompt_tokens':300,'completion_tokens':50,'total_tokens':350,'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':300}})
    for task in tasks:
        fixtures=FixtureTools(tools,task['scenario']); records=[]
        transport=MockPlannerTransport(handler)
        try:
            policy=DeepSeekPlanner(transport)
            controller=PlanningHarness(task,fixtures,verifier,fingerprint(manifest),WORKSPACE/'temp')
            resumed=task['scenario']=='resume_after_relations'
            if resumed:
                controller.step(policy); records.extend(policy.records)
                checkpoint=output.with_name(output.stem+'_'+task['id']+'_checkpoint.json')
                controller.checkpoint(checkpoint)
                controller=PlanningHarness.restore(checkpoint,task,fixtures,verifier,fingerprint(manifest),WORKSPACE/'temp')
                policy=DeepSeekPlanner(transport)
            result=controller.run(policy); records.extend(policy.records)
            cases.append({'task_id':task['id'],'policy':'deepseek_sdk_mock','resumed':resumed,'tool_calls':fixtures.calls,
                          'result':result,'planner_records':records})
        finally:transport.close()
    score(cases,bundle)
    assert all(x['score']['passed'] for x in cases)
    assert all(sha_file(p)==before[str(p.relative_to(WORKSPACE))] for p in protected)
    report={'status':'passed','mode':'offline_snapshot_sdk_http_mock','cases':cases,'captured_requests':requests,
            'mock_sdk_requests':len(requests),'external_model_calls':0,'database_access':False,'answers_generated':0,
            'usage_is_simulated':True,'protected_hashes_unchanged':before,
            'limitations':['Mock server returns the known rule action, not real model decisions.','Reported tokens are fabricated SDK fixtures, not real billing.']}
    with output.open('x',encoding='utf-8') as stream:json.dump(report,stream,ensure_ascii=False,indent=2)
    print(json.dumps({'status':'passed','tasks':len(cases),'mock_sdk_requests':len(requests),'external_model_calls':0}))


if __name__=='__main__':main()
