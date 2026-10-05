"""Behavior/security tests for isolated planning and short-lived task memory."""
import copy
import json
import unittest
import uuid
from pathlib import Path
from .planning_runtime import PlanningHarness, BoundaryError, entity_id, fingerprint
from .planning_policies import RulePolicy, MockDynamicPolicy, SinglePlanPolicy

TEMP = Path(__file__).resolve().parents[3] / 'temp'
A, B, C = ['甲疾病', '乙疾病', '丙疾病']
# Unicode ordering is 丙 before 乙.
NAMES = sorted([B, C])
HASH = 'a' * 64


class Clock:
    def __init__(self): self.value = 1000.
    def __call__(self): return self.value
    def advance(self, value): self.value += value


class Verifier:
    source_hash = HASH
    def check_entity(self, eid, name):
        if name not in (A, B, C) or eid != entity_id(name): raise BoundaryError('entity')
    def check_proof(self, eid, name, field, value, source, target=None):
        self.check_entity(eid, name)
        if source != {'owner': name, 'field': field}: raise BoundaryError('source')
        if field == 'acompany_with': self.check_entity(target, value)
        elif value != name + ':' + field: raise BoundaryError('text')
        return False


class Tools:
    def __init__(self, mode='normal', clock=None):
        self.mode, self.clock, self.calls = mode, clock, []
    def read(self, action, timeout):
        self.calls.append(copy.deepcopy(action))
        if self.mode == 'late' and self.clock: self.clock.advance(91)
        if self.mode == 'throw': raise RuntimeError('secret credentials must not leak')
        if self.mode == 'permanent': return {'dataset_id': 'medical_full_v1', 'status': 'permanent_error'}
        if action['kind'] == 'read_relations':
            return {'dataset_id': 'medical_full_v1', 'status': 'found',
                    'entities': [{'id': entity_id(n), 'name': n, 'source': {'owner': A, 'field': 'acompany_with'}} for n in NAMES]}
        if self.mode == 'transient' and len(self.calls) == 2 or self.mode == 'always_transient':
            return {'dataset_id': 'medical_full_v1', 'status': 'transient_error'}
        eid = action['entity_ids'][0]
        name = next(n for n in NAMES if entity_id(n) == eid)
        fields = {f: {'status': 'found', 'value': name + ':' + f, 'source': {'owner': name, 'field': f}, 'truncated': False} for f in action['fields']}
        if self.mode == 'missing' and name == NAMES[0] and 'prevent' in fields: fields['prevent'] = {'status': 'missing'}
        if self.mode == 'bad_source': fields['desc']['source']['owner'] = A
        if self.mode == 'wrong_entity': eid = entity_id(A)
        if self.mode == 'false_truncated': fields['desc']['truncated'] = True
        return {'dataset_id': 'medical_full_v1', 'status': 'found', 'entity_id': eid, 'fields': fields}


class Actions:
    local_only = True
    decision_cost = 1
    def __init__(self, action): self.action = action
    def decide(self, context, timeout): return copy.deepcopy(self.action)


class PlanningRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.task = {'id': 'P01', 'question': A + '的并发对象资料', 'seed': {'id': entity_id(A), 'name': A},
                     'budget': {'read_calls': 5, 'planner_decisions': 4, 'answer_calls': 1, 'total_seconds': 90}}
        self.tools = Tools()
        self.controller = self.make(self.tools)
    def make(self, tools):
        return PlanningHarness(self.task, tools, Verifier(), 'manifest', TEMP, self.clock, self.clock)
    def checkpoint(self):
        path = TEMP / ('planning_test_' + uuid.uuid4().hex + '.json')
        self.controller.checkpoint(path)
        return path
    def restore(self, path, task=None):
        return PlanningHarness.restore(path, task or self.task, self.tools, Verifier(), 'manifest', TEMP, self.clock, self.clock)
    def alter_checkpoint(self, path, change):
        data = json.loads(path.read_text(encoding='utf-8'))
        change(data['payload'])
        data['sha256'] = fingerprint(data['payload'])
        path.write_text(json.dumps(data), encoding='utf-8')

    def test_normal_requires_discovery_and_keeps_target_sources(self):
        policy = MockDynamicPolicy()
        result = self.controller.run(policy)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['counts'], {'reads': 3, 'decisions': 3, 'answers': 0})
        self.assertEqual(policy.inputs[0]['selected_ids'], [])
        self.assertEqual(len(policy.inputs[1]['selected_ids']), 2)
        for row in result['selected']:
            for field in row['fields'].values():
                evidence = result['evidence'][field['evidence_id']]
                self.assertEqual(evidence['source']['owner'], row['name'])

    def test_missing_preserves_other_fields_and_stops_partial(self):
        result = self.make(Tools('missing')).run(MockDynamicPolicy())
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['selected'][0]['fields']['prevent']['status'], 'missing')
        self.assertEqual(result['selected'][0]['fields']['desc']['status'], 'found')
        self.assertEqual(result['counts']['reads'], 3)

    def test_transient_retry_is_charged_once_and_recovers(self):
        tools = Tools('transient'); result = self.make(tools).run(MockDynamicPolicy())
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['counts'], {'reads': 4, 'decisions': 4, 'answers': 0})
        self.assertEqual(tools.calls[1], tools.calls[2])

    def test_second_transient_stops(self):
        result = self.make(Tools('always_transient')).run(MockDynamicPolicy())
        self.assertEqual(result['stop_reason'], 'transient_retry_exhausted')
        self.assertEqual(result['counts']['reads'], 3)

    def test_arbitrary_cypher_never_reaches_tools(self):
        result = self.controller.run(Actions({'kind': 'cypher', 'query': 'CREATE (n)'}))
        self.assertEqual(self.tools.calls, [])
        self.assertEqual(result['stop_reason'], 'decision_budget')
        self.assertEqual(result['counts']['decisions'], 4)

    def test_legal_looking_but_undiscovered_id_rejected(self):
        result = self.controller.run(Actions({'kind': 'read_fields', 'entity_ids': [entity_id(B)], 'fields': ['desc']}))
        self.assertEqual(self.tools.calls, [])
        self.assertEqual(result['status'], 'blocked')

    def test_extra_parameters_rejected(self):
        action = {'kind': 'read_relations', 'entity_id': entity_id(A), 'relation': 'acompany_with', 'limit': 9000}
        self.controller.run(Actions(action)); self.assertEqual(self.tools.calls, [])

    def test_model_cannot_claim_completion_before_evidence(self):
        self.controller.run(Actions({'kind': 'stop', 'status': 'complete', 'reason': 'done', 'evidence_ids': []}))
        self.assertEqual(self.controller.state['phase'], 'blocked')
        self.assertEqual(self.tools.calls, [])

    def test_foreign_citation_rejected(self):
        self.controller.step(RulePolicy())
        self.controller.run(Actions({'kind': 'stop', 'status': 'blocked', 'reason': 'x', 'evidence_ids': ['E999']}))
        self.assertEqual(self.controller.state['phase'], 'blocked')

    def test_successful_relation_read_not_repeatable(self):
        policy = Actions({'kind': 'read_relations', 'entity_id': entity_id(A), 'relation': 'acompany_with'})
        self.controller.run(policy)
        self.assertEqual(len(self.tools.calls), 1)

    def test_bad_source_rejects_entire_observation(self):
        result = self.make(Tools('bad_source')).run(MockDynamicPolicy())
        self.assertEqual(result['stop_reason'], 'tool_or_source_invalid')
        self.assertEqual(len(result['evidence']), 2)  # Only validated relation proofs remain.
        self.assertEqual(result['selected'][0]['fields']['desc']['status'], 'pending')

    def test_wrong_entity_tool_result_rejected(self):
        self.assertEqual(self.make(Tools('wrong_entity')).run(RulePolicy())['status'], 'blocked')

    def test_false_truncation_marker_rejected(self):
        self.assertEqual(self.make(Tools('false_truncated')).run(RulePolicy())['status'], 'blocked')

    def test_tool_exception_is_safe_and_charged(self):
        result = self.make(Tools('throw')).run(MockDynamicPolicy())
        self.assertEqual(result['counts']['reads'], 1)
        self.assertNotIn('credentials', json.dumps(result))

    def test_permanent_error_not_retried(self):
        tools = Tools('permanent'); result = self.make(tools).run(RulePolicy())
        self.assertEqual(result['stop_reason'], 'permanent_tool_error')
        self.assertEqual(len(tools.calls), 1)

    def test_late_observation_not_ingested(self):
        result = self.make(Tools('late', self.clock)).run(RulePolicy())
        self.assertEqual(result['stop_reason'], 'deadline')
        self.assertEqual(result['evidence'], {})

    def test_deadline_checked_before_action(self):
        self.clock.advance(91)
        self.controller.run(RulePolicy()); self.assertEqual(self.tools.calls, [])

    def test_read_budget_counts_entities_not_batches(self):
        self.task['budget']['read_calls'] = 2
        result = self.make(self.tools).run(RulePolicy())
        self.assertEqual(result['stop_reason'], 'read_budget')
        self.assertEqual(len(self.tools.calls), 1)

    def test_checkpoint_resumes_with_counts_and_no_repeat(self):
        self.controller.step(MockDynamicPolicy())
        path = self.checkpoint(); self.clock.advance(2)
        restored = self.restore(path)
        self.assertLessEqual(restored.remaining(), 88)
        result = restored.run(MockDynamicPolicy())
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['counts'], {'reads': 3, 'decisions': 3, 'answers': 0})
        self.assertEqual(sum(x['kind'] == 'read_relations' for x in self.tools.calls), 1)

    def test_checkpoint_integrity_damage_rejected(self):
        self.controller.step(RulePolicy()); path = self.checkpoint()
        data = json.loads(path.read_text(encoding='utf-8')); data['payload']['remaining'] = 80
        path.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(BoundaryError): self.restore(path)

    def test_checkpoint_task_isolation(self):
        self.controller.step(RulePolicy()); path = self.checkpoint()
        task = copy.deepcopy(self.task); task['id'] = 'P02'
        with self.assertRaises(BoundaryError): self.restore(path, task)

    def test_checkpoint_expiration(self):
        self.controller.step(RulePolicy()); path = self.checkpoint(); self.clock.advance(91)
        with self.assertRaises(BoundaryError): self.restore(path)

    def test_checkpoint_cannot_reset_budget_even_with_rehashed_envelope(self):
        self.controller.step(MockDynamicPolicy()); path = self.checkpoint()
        self.alter_checkpoint(path, lambda x: x['state']['counts'].update(reads=0))
        with self.assertRaises(BoundaryError): self.restore(path)

    def test_checkpoint_missing_evidence_rejected(self):
        self.controller.step(RulePolicy()); path = self.checkpoint()
        self.alter_checkpoint(path, lambda x: x['state']['evidence'].pop('E1'))
        with self.assertRaises(BoundaryError): self.restore(path)

    def test_checkpoint_foreign_registry_rejected(self):
        self.controller.step(RulePolicy()); path = self.checkpoint()
        self.alter_checkpoint(path, lambda x: x['state']['registry'][entity_id(B)].update(name='外来疾病'))
        with self.assertRaises(BoundaryError): self.restore(path)

    def test_checkpoint_write_outside_temp_rejected(self):
        with self.assertRaises(BoundaryError): self.controller.checkpoint(TEMP.parent / 'must_not_create.json')

    def test_remote_policy_is_disabled(self):
        class RemotePolicy: local_only = False
        with self.assertRaises(BoundaryError): self.controller.step(RemotePolicy())
        self.assertEqual(self.tools.calls, [])

    def test_mock_context_contains_no_oracle_or_full_source_text(self):
        policy = MockDynamicPolicy(); self.controller.run(policy)
        serialized = json.dumps(policy.inputs)
        self.assertNotIn('oracle', serialized)
        self.assertNotIn('甲疾病:desc', serialized)
        self.assertNotIn('owner', serialized)

    def test_single_plan_stub_does_not_add_decision_calls(self):
        result = self.controller.run(SinglePlanPolicy())
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['counts']['decisions'], 1)

    def test_cooperative_planner_timeout_discards_action(self):
        clock = self.clock
        class Slow(MockDynamicPolicy):
            def decide(self, context, timeout): clock.advance(16); return super().decide(context, timeout)
        result = self.controller.run(Slow())
        self.assertEqual(result['stop_reason'], 'planner_timeout')
        self.assertEqual(self.tools.calls, [])

    def test_checkpoint_after_transient_preserves_retry_and_decision_cost(self):
        self.tools = Tools('transient')
        self.controller = self.make(self.tools)
        policy = MockDynamicPolicy()
        self.controller.step(policy)
        self.controller.step(policy)
        path = self.checkpoint()
        result = self.restore(path).run(MockDynamicPolicy())
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['counts'], {'reads': 4, 'decisions': 4, 'answers': 0})

    def test_unknown_tool_status_does_not_become_evidence(self):
        class InvalidStatus(Tools):
            def read(self, action, timeout):
                result = super().read(action, timeout)
                result['status'] = 'untrusted_status'
                return result
        result = self.make(InvalidStatus()).run(RulePolicy())
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['evidence'], {})

    def test_boolean_checkpoint_count_is_not_an_integer_budget(self):
        self.controller.step(MockDynamicPolicy()); path = self.checkpoint()
        self.alter_checkpoint(path, lambda x: x['state']['counts'].update(reads=True))
        with self.assertRaises(BoundaryError): self.restore(path)
