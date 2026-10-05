"""Replay recorded failures and a scripted contract-aware correction, fully offline.

Corrected mock is NOT a model-quality evaluation. No live execution flag exists.
"""
import argparse,copy,json,sys
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1];WORKSPACE=PROJECT.parent
sys.path.insert(0,str(PROJECT/'knowledge_graph_backend'))
from api.planning_runtime import PlanningHarness,fingerprint
from api.deepseek_planner import DeepSeekPlanner,MockPlannerTransport
from api.planning_tools import SourceCatalog,SnapshotTools,sha_file
from evaluate_planning_runtime import FixtureTools,score


class RecordedPolicy:
    local_only=True;decision_cost=1
    def __init__(self,actions):self.actions=iter(actions)
    def decide(self,context,timeout):return copy.deepcopy(next(self.actions))


def main():
    import httpx2 as httpx
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--report',required=True,type=Path)
    args=parser.parse_args();output=args.report.resolve()
    if not output.is_relative_to(WORKSPACE/'temp') or output.exists():parser.error('New temp report required')
    frozen_path=PROJECT/'data/planning_failure_v1/cases.json';frozen=json.loads(frozen_path.read_text(encoding='utf-8'))
    assert sha_file(WORKSPACE/frozen['source_report'])==frozen['source_sha256']
    bundle=PROJECT/'data/planning_challenge_v1';manifest=json.loads((bundle/'manifest.json').read_text(encoding='utf-8'))
    for path,sha in manifest['hashes'].items():assert sha_file(WORKSPACE/path)==sha
    taskmap={t['id']:t for t in json.loads((bundle/'tasks.json').read_text(encoding='utf-8'))['tasks']}
    protected=[PROJECT/'data/medical.json',PROJECT/'knowledge_graph_backend/db.sqlite3']
    before={str(p.relative_to(WORKSPACE)):sha_file(p) for p in protected}
    verifier=SourceCatalog(PROJECT/'data',manifest['hashes']['bishe/data/medical.json'])
    tools=SnapshotTools(verifier,[taskmap[f['task_id']]['seed']['id'] for f in frozen['cases']]);results=[]
    for failure in frozen['cases']:
        task=taskmap[failure['task_id']];actions=failure['recorded_actions']
        old_tools=FixtureTools(tools,task['scenario'])
        old=PlanningHarness(task,old_tools,verifier,fingerprint(manifest),WORKSPACE/'temp')
        original=old.run(RecordedPolicy(actions))
        assert original['status']==failure['original_status']
        assert [e['reason'] for e in original['trace'] if e['event']=='rejected']==failure['recorded_rejections']
        results.append({'task_id':task['id'],'policy':'recorded_actions','resumed':False,'tool_calls':old_tools.calls,'result':original})
        captured=[]
        def handler(request):
            body=json.loads(request.content);state=json.loads(body['messages'][1]['content'])['task_state'];captured.append(state)
            index=len(captured)-1
            if index<3:action=actions[index]
            else:
                assert state['last_action_result']=={'status':'rejected','reason_code':failure['recorded_rejections'][0]}
                assert state['stop_ready'] and state['required_stop_evidence_ids']
                assert state['readable_fields']==[]
                if task['id']=='P05':assert state['handled_missing_fields'][0]['retry_allowed'] is False
                action={'kind':'stop','status':state['stop_status'],'reason':'模拟客户端根据反馈纠正停止动作','evidence_ids':state['required_stop_evidence_ids']}
            return httpx.Response(200,json={'id':'mock-repair','created':0,'object':'chat.completion','model':'deepseek-flash',
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(action,ensure_ascii=False)}}]})
        transport=MockPlannerTransport(handler)
        try:
            policy=DeepSeekPlanner(transport);fixture=FixtureTools(tools,task['scenario'])
            corrected=PlanningHarness(task,fixture,verifier,fingerprint(manifest),WORKSPACE/'temp').run(policy)
            assert corrected['status']==('partial' if task['id']=='P05' else 'complete')
            assert corrected['counts']['decisions']==4 and corrected['counts']['reads']==original['counts']['reads']
            assert any(e['event']=='rejected' for e in corrected['trace'])  # Original bad action is still forbidden.
            results.append({'task_id':task['id'],'policy':'scripted_feedback_correction','resumed':False,'tool_calls':fixture.calls,
                            'result':corrected,'captured_states':captured,'planner_records':policy.records})
        finally:transport.close()
    score(results,bundle)
    # Full scorer deliberately fails any run containing an invalid action. Even a
    # later mock repair must not be called a clean benchmark success.
    assert all(not row['score']['passed'] for row in results)
    assert all(sha_file(p)==before[str(p.relative_to(WORKSPACE))] for p in protected)
    report={'status':'contract_passed','cases':results,'external_model_calls':0,'database_access':False,
            'failure_fixture_sha256':sha_file(frozen_path),'protected_hashes_unchanged':before,
            'limitations':['Scripted mock correction demonstrates feedback reachability, not learned recovery.',
                           'Original rejected action retained; strict benchmark still fails both repaired trajectories.',
                           'No claim that live 2/4 score improved.']}
    with output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps({'status':report['status'],'frozen_failures':2,'scripted_corrections':2,'external_model_calls':0}))


if __name__=='__main__':main()
