"""Evaluate isolated local policies. Default offline; --read-only opts into Neo4j.

Oracle is opened only by the post-run scorer, never by the planner/controller.
"""
import argparse
import copy
import json
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT.parent
sys.path.insert(0, str(PROJECT / 'knowledge_graph_backend'))
from api.planning_runtime import PlanningHarness, fingerprint
from api.planning_tools import SourceCatalog, SnapshotTools, ReadOnlyTools, sha_file, location
from api.planning_policies import RulePolicy, MockDynamicPolicy, SinglePlanPolicy


class FixtureTools:
    def __init__(self, tools, scenario):
        self.tools, self.scenario = tools, scenario
        self.calls = []
        self.adapter_calls = 0
        self.failed_once = False
        self.first_selected = None
    def read(self, action, timeout):
        self.calls.append(copy.deepcopy(action))
        if self.scenario == 'transient_error' and action['kind'] == 'read_fields' and not self.failed_once:
            self.failed_once = True
            return {'dataset_id': 'medical_full_v1', 'status': 'transient_error'}
        self.adapter_calls += 1
        result = copy.deepcopy(self.tools.read(action, timeout))
        if action['kind'] == 'read_relations': self.first_selected = result['entities'][0]['id']
        elif self.scenario == 'missing_prevent' and action['entity_ids'][0] == self.first_selected:
            result['fields']['prevent'] = {'status': 'missing'}
        return result


def score(results, bundle):
    # Scoring is deliberately after ALL controller runs and reads a separate file.
    gold = {x['id']: x for x in json.loads((bundle / 'oracle.json').read_text(encoding='utf-8'))['cases']}
    for item in results:
        result, expected = item['result'], gold[item['task_id']]
        errors = []
        if result['status'] != expected['expected_stop']: errors.append('stop')
        if [x['id'] for x in result['selected']] != [x['id'] for x in expected['selected']]: errors.append('selection')
        covered = 0
        for actual, entity in zip(result['selected'], expected['selected']):
            for field in entity['fields']:
                observed = actual['fields'].get(field['field'], {})
                if observed.get('status') != field['expected_status']:
                    errors.append('field_status'); continue
                if field['expected_status'] == 'found':
                    proof = result['evidence'].get(observed.get('evidence_id'), {})
                    if (proof.get('entity_id') != entity['id'] or proof.get('value') != field['available_value']
                            or proof.get('source') not in [location(x) for x in field['sources']] or proof.get('truncated') != field['truncated']):
                        errors.append('evidence'); continue
                covered += 1
        if covered != sum(len(e['fields']) for e in expected['selected']): errors.append('coverage')
        if any(e['event'] == 'rejected' for e in result['trace']): errors.append('invalid_action')
        if item['resumed'] and sum(a['kind'] == 'read_relations' for a in item['tool_calls']) != 1:
            errors.append('repeated_relation_after_resume')
        if item['resumed'] and result['counts']['reads'] != len(item['tool_calls']): errors.append('reset_counts')
        if result['counts']['reads'] > 5 or result['counts']['decisions'] > 4: errors.append('budget')
        item['score'] = {'passed': not errors, 'errors': errors, 'covered': covered,
                         'total': sum(len(e['fields']) for e in expected['selected'])}
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--read-only', action='store_true')
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    report_path = args.report.resolve()
    if not report_path.is_relative_to(WORKSPACE / 'temp') or report_path.exists():
        parser.error('Report must be a new file inside project temp')
    bundle = PROJECT / 'data/planning_challenge_v1'
    manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
    for path, sha in manifest['hashes'].items():
        if sha_file(WORKSPACE / path) != sha: raise ValueError('Frozen input changed: ' + path)
    protected = [PROJECT / 'data/medical.json', PROJECT / 'knowledge_graph_backend/db.sqlite3']
    before = {str(p.relative_to(WORKSPACE)): sha_file(p) for p in protected}
    source_hash = manifest['hashes']['bishe/data/medical.json']
    tasks = json.loads((bundle / 'tasks.json').read_text(encoding='utf-8'))['tasks']
    verifier = SourceCatalog(PROJECT / 'data', source_hash)
    if args.read_only:
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'knowledge_graph_backend.settings')
        import django
        django.setup()
        tools = ReadOnlyTools()
    else:
        tools = SnapshotTools(verifier, [t['seed']['id'] for t in tasks])
    policies = {'rule': RulePolicy, 'single_plan_stub': SinglePlanPolicy, 'dynamic_stub': MockDynamicPolicy}
    results = []
    for name, cls in policies.items():
        for task in tasks:
            fixtures = FixtureTools(tools, task['scenario'])
            controller = PlanningHarness(task, fixtures, verifier, fingerprint(manifest), WORKSPACE / 'temp')
            policy = cls()
            resumed = task['scenario'] == 'resume_after_relations'
            if resumed:
                controller.step(policy)
                checkpoint = report_path.with_name(report_path.stem + '_' + name + '_' + task['id'] + '_checkpoint.json')
                controller.checkpoint(checkpoint)
                controller = PlanningHarness.restore(checkpoint, task, fixtures, verifier, fingerprint(manifest), WORKSPACE / 'temp')
                policy = cls(resumed=True) if name == 'single_plan_stub' else cls()
            result = controller.run(policy)
            results.append({'policy': name, 'task_id': task['id'], 'split': task['split'], 'scenario': task['scenario'],
                            'resumed': resumed, 'tool_calls': fixtures.calls, 'adapter_calls': fixtures.adapter_calls,
                            'elapsed_ms': int(1000 * (task['budget']['total_seconds'] - controller.remaining())), 'result': result})
    score(results, bundle)
    after = {str(p.relative_to(WORKSPACE)): sha_file(p) for p in protected}
    assert before == after
    summary = {name: {'passed': sum(x['score']['passed'] for x in results if x['policy'] == name), 'total': len(tasks),
               'read_calls': sum(x['result']['counts']['reads'] for x in results if x['policy'] == name),
               'local_decision_calls': sum(x['result']['counts']['decisions'] for x in results if x['policy'] == name),
               'adapter_calls': sum(x['adapter_calls'] for x in results if x['policy'] == name),
               'neo4j_read_transactions': sum(x['adapter_calls'] for x in results if x['policy'] == name) if args.read_only else 0} for name in policies}
    report = {'status': 'passed' if all(x['score']['passed'] for x in results) else 'failed',
              'mode': 'mock_policies_read_only_tools' if args.read_only else 'mock_policies_offline_snapshot',
              'summary': summary, 'cases': results, 'protected_hashes_unchanged': before, 'external_model_calls': 0,
              'database_writes': 0, 'answers_generated': 0, 'fixtures_executed': True, 'model_token_usage': None, 'model_latency_ms': None,
              'limitations': ['All policies are local authored stubs, not real LLM decisions.',
                             'Single-plan stub uses a fixed symbolic plan and predeclared retry, not a measured model plan.',
                             'Development/holdout partition is retained; these are contract checks, not model holdout scores.',
                             'No evidence that dynamic planning improves task success; equal mock results are expected.',
                             'Short-lived task memory only; no long-term or account history memory.']}
    with report_path.open('x', encoding='utf-8') as stream: json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'status': report['status'], 'mode': report['mode'], 'summary': summary, 'external_model_calls': 0}, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__': sys.exit(main())
