"""Source-verified bounded planning harness, reused by formal chat and evaluation.

Actions come from an injected policy; no SDK/model API is implemented here.
External policies still require explicit allow_external and share the same budgets.
"""
import copy
import hashlib
import json
import math
import time
from pathlib import Path

DATASET = 'medical_full_v1'
FIELDS = ('desc', 'prevent')


def entity_id(name):
    return 'Disease:' + hashlib.sha256(('Disease\0' + name).encode()).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class BoundaryError(ValueError):
    pass


SAFE_REJECTIONS = frozenset({
    'action_object', 'action_schema', 'relation_scope', 'unregistered_or_unselected_entity',
    'fields', 'resolved_field_repeat', 'stop_schema_or_cross_task_evidence', 'premature_stop',
    'incomplete_citations', 'duplicate_or_retry_exhausted', 'planner_timeout_budget',
    'planner_context', 'planner_question_limit', 'planner_registry_limit', 'planner_requirements',
    'planner_input_bytes', 'planner_output_length', 'planner_invalid_json', 'planner_action_schema',
    'planner_duplicate_key', 'planner_nonfinite_json', 'planner_incomplete_response',
    'planner_unexpected_tool_calls', 'planner_late_response', 'planner_transport_error',
    'planner_call_budget', 'batch_budget_or_halted', 'authorized_transport_error',
})


def feedback_contract(public):
    """Derive control requirements from current state, never from model claims."""
    selected = public['selected_ids']
    required = {e for row in public['registry'] if row['id'] in selected for e in row['evidence_ids']}
    missing, readable, statuses = [], [], []
    for eid, row in public['requirements'].items():
        pending = []
        for field, item in row.items():
            status = item['status']; statuses.append(status)
            if status == 'found': required.add(item['evidence_id'])
            elif status == 'missing':
                missing.append({'entity_id': eid, 'field': field, 'handled': True, 'retry_allowed': False})
            elif status in ('pending', 'transient_error'): pending.append(field)
        if pending: readable.append({'entity_id': eid, 'fields': pending})
    ready = bool(statuses) and all(x in ('found', 'missing') for x in statuses)
    previous = public.get('last_action_result', {})
    status = previous.get('status', 'none') if isinstance(previous, dict) else 'none'
    if status not in ('none', 'found', 'transient_error', 'rejected'): status = 'none'
    feedback = {'status': status}
    if status == 'rejected':
        reason = previous.get('reason_code')
        feedback['reason_code'] = reason if isinstance(reason, str) and reason in SAFE_REJECTIONS else 'action_rejected'
    return {'last_action_result': feedback,
            'required_stop_evidence_ids': sorted(required, key=lambda x: int(x[1:])),
            'stop_ready': ready, 'stop_status': ('partial' if missing else 'complete') if ready else None,
            'handled_missing_fields': missing, 'readable_fields': readable}


class PlanningHarness:
    def __init__(self, task, tools, verifier, manifest_hash, temp_root, clock=time.monotonic, wall=time.time, allow_external=False):
        self.allow_external = allow_external is True
        self.task = copy.deepcopy(task)
        self.tools, self.verifier = tools, verifier
        self.manifest_hash = manifest_hash
        self.temp_root = Path(temp_root).resolve()
        self.clock, self.wall = clock, wall
        budget = task['budget']
        if any(type(budget[k]) is not int or not 0 < budget[k] <= cap for k, cap in
               [('read_calls', 5), ('planner_decisions', 4), ('answer_calls', 1), ('total_seconds', 90)]):
            raise BoundaryError('budget')
        seed = task['seed']
        if seed['id'] != entity_id(seed['name']) or seed['name'] not in task['question']:
            raise BoundaryError('seed')
        self.verifier.check_entity(seed['id'], seed['name'])
        self.deadline = clock() + budget['total_seconds']
        self.expires_at = wall() + budget['total_seconds']
        self.state = {'version': 'planning_task_runtime_v1', 'task_id': task['id'], 'dataset_id': DATASET,
                      'source_sha256': verifier.source_hash, 'registry': {seed['id']: {**seed, 'origin': 'explicit_question', 'evidence_ids': []}},
                      'selected_ids': [], 'requirements': {}, 'evidence': {}, 'observations': [],
                      'completed': [], 'attempts': {}, 'counts': {'reads': 0, 'decisions': 0, 'answers': 0},
                      'trace': [], 'phase': 'active', 'stop_reason': ''}

    def remaining(self):
        return max(0., self.deadline - self.clock())

    def block(self, reason):
        self.state['phase'], self.state['stop_reason'] = 'blocked', reason

    def evidence(self, entity, field, value, source, target=None, truncated=False):
        expected_truncation = self.verifier.check_proof(entity['id'], entity['name'], field, value, source, target)
        if type(truncated) is not bool or truncated != expected_truncation:
            raise BoundaryError('truncation_mismatch')
        key = 'E' + str(len(self.state['evidence']) + 1)
        self.state['evidence'][key] = {'id': key, 'entity_id': entity['id'], 'field': field, 'value': value,
                                      'source': copy.deepcopy(source), 'target_id': target,
                                      'truncated': truncated, 'medical_review_status': 'not_verified'}
        return key

    def validate(self, action):
        if not isinstance(action, dict):
            raise BoundaryError('action_object')
        kind = action.get('kind')
        allowed = {'read_relations': {'kind', 'entity_id', 'relation'},
                   'read_fields': {'kind', 'entity_ids', 'fields'},
                   'stop': {'kind', 'status', 'reason', 'evidence_ids'}}
        if kind not in allowed or set(action) != allowed[kind]:
            raise BoundaryError('action_schema')
        if kind == 'read_relations':
            if action['entity_id'] != self.task['seed']['id'] or action['relation'] != 'acompany_with':
                raise BoundaryError('relation_scope')
        elif kind == 'read_fields':
            ids, fields = action['entity_ids'], action['fields']
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 2 or any(not isinstance(x, str) for x in ids)
                    or len(set(ids)) != len(ids) or any(x not in self.state['selected_ids'] for x in ids)):
                raise BoundaryError('unregistered_or_unselected_entity')
            if (not isinstance(fields, list) or not 1 <= len(fields) <= 2 or any(x not in FIELDS for x in fields)
                    or len(set(fields)) != len(fields)):
                raise BoundaryError('fields')
            if any(self.state['requirements'][eid][f]['status'] in ('found', 'missing') for eid in ids for f in fields):
                raise BoundaryError('resolved_field_repeat')
        else:
            refs = action['evidence_ids']
            if (action['status'] not in ('complete', 'partial', 'blocked') or not isinstance(action['reason'], str)
                    or len(action['reason']) > 240 or not isinstance(refs, list) or len(refs) > 32
                    or any(not isinstance(x, str) or x not in self.state['evidence'] for x in refs) or len(set(refs)) != len(refs)):
                raise BoundaryError('stop_schema_or_cross_task_evidence')
            statuses = [x['status'] for row in self.state['requirements'].values() for x in row.values()]
            expected = ('complete' if statuses and all(x == 'found' for x in statuses) else
                        'partial' if statuses and all(x in ('found', 'missing') for x in statuses) else 'blocked')
            if action['status'] != expected:
                raise BoundaryError('premature_stop')
            required = set(feedback_contract({'selected_ids': self.state['selected_ids'],
                'registry': list(self.state['registry'].values()), 'requirements': self.state['requirements']})['required_stop_evidence_ids'])
            if action['status'] != 'blocked' and not required.issubset(refs):
                raise BoundaryError('incomplete_citations')
        return kind

    def ingest(self, action, result):
        if not isinstance(result, dict) or result.get('dataset_id') != DATASET or result.get('status') != 'found':
            raise BoundaryError('tool_dataset')
        if action['kind'] == 'read_relations':
            if result.get('status') != 'found' or not isinstance(result.get('entities'), list) or not result['entities']:
                raise BoundaryError('relation_unavailable')
            rows = result['entities']
            if len(rows) > 8 or [x['name'] for x in rows] != sorted(x['name'] for x in rows) or len({x['id'] for x in rows}) != len(rows):
                raise BoundaryError('relation_order_or_limit')
            seed = self.task['seed']
            for row in rows:
                self.verifier.check_entity(row['id'], row['name'])
                proof = self.evidence(seed, 'acompany_with', row['name'], row['source'], row['id'])
                self.state['registry'][row['id']] = {'id': row['id'], 'name': row['name'], 'origin': 'relation_result', 'evidence_ids': [proof]}
            self.state['selected_ids'] = [x['id'] for x in rows[:2]]
            self.state['requirements'] = {eid: {f: {'status': 'pending'} for f in FIELDS} for eid in self.state['selected_ids']}
        else:
            eid = action['entity_ids'][0]
            if result.get('entity_id') != eid or set(result.get('fields', {})) != set(action['fields']):
                raise BoundaryError('tool_entity_or_fields')
            entity = self.state['registry'][eid]
            for field, item in result['fields'].items():
                if item.get('status') == 'missing':
                    self.state['requirements'][eid][field] = {'status': 'missing'}
                elif item.get('status') == 'found':
                    value = item.get('value')
                    if not isinstance(value, str) or not value.strip() or len(value) > 1800 or type(item.get('truncated')) is not bool:
                        raise BoundaryError('invalid_excerpt')
                    proof = self.evidence(entity, field, value, item['source'], truncated=item['truncated'])
                    self.state['requirements'][eid][field] = {'status': 'found', 'evidence_id': proof}
                else:
                    raise BoundaryError('field_status')

    def execute(self, action):
        kind = self.validate(action)
        if self.remaining() <= 0:
            self.block('deadline'); return
        if kind == 'stop':
            self.state['phase'], self.state['stop_reason'] = action['status'], action['reason']
            self.state['trace'].append({'event': 'stop', 'action': copy.deepcopy(action)})
            return
        pieces = ([action] if kind == 'read_relations' else
                  [{**action, 'entity_ids': [eid]} for eid in action['entity_ids']])
        if self.state['counts']['reads'] + len(pieces) > self.task['budget']['read_calls']:
            self.block('read_budget'); return
        for part in pieces:
            key = canonical(part)
            if key in self.state['completed'] or self.state['attempts'].get(key, 0) >= 2:
                raise BoundaryError('duplicate_or_retry_exhausted')
            if self.remaining() <= 0:
                self.block('deadline'); return
            self.state['counts']['reads'] += 1
            self.state['attempts'][key] = self.state['attempts'].get(key, 0) + 1
            before = copy.deepcopy(self.state)
            try:
                result = self.tools.read(part, timeout=min(30, self.remaining()))
                if self.remaining() <= 0:
                    self.block('deadline'); return
                if result.get('status') == 'transient_error':
                    if kind != 'read_fields' or self.state['attempts'][key] >= 2:
                        self.block('transient_retry_exhausted'); return
                    eid = part['entity_ids'][0]
                    for f in part['fields']:
                        self.state['requirements'][eid][f] = {'status': 'transient_error'}
                elif result.get('status') == 'permanent_error':
                    self.block('permanent_tool_error'); return
                else:
                    self.ingest(part, result)
                    self.state['completed'].append(key)
            except Exception:
                self.state = before  # Reject the entire observation; retain spent counts/attempt.
                self.block('tool_or_source_invalid'); return
            self.state['observations'].append({'action': copy.deepcopy(part), 'result': copy.deepcopy(result)})
            self.state['trace'].append({'event': 'read', 'action': copy.deepcopy(part), 'status': result['status']})
            if result['status'] == 'transient_error':
                break

    def context(self):
        public = {'task_id': self.task['id'], 'question': self.task['question'],
                  'seed_id': self.task['seed']['id'], 'selected_ids': self.state['selected_ids'],
                  'registry': list(self.state['registry'].values()), 'requirements': self.state['requirements'],
                  'evidence_ids': list(self.state['evidence']), 'counts': self.state['counts'],
                  'remaining_ms': int(self.remaining() * 1000)}
        # The trace persists feedback across checkpoint without copying raw exceptions.
        for event in reversed(self.state['trace']):
            if event['event'] == 'rejected':
                public['last_action_result'] = {'status': 'rejected', 'reason_code': event['reason']}
                break
            if event['event'] == 'read':
                public['last_action_result'] = {'status': event['status']}
                break
        public.update(feedback_contract(public))
        # No evidence body or oracle is sent.
        if len(canonical(public)) > 12000:
            raise BoundaryError('context_budget')
        return copy.deepcopy(public)

    def step(self, policy):
        if self.state['phase'] != 'active':
            return
        if self.remaining() <= 0:
            self.block('deadline'); return
        if getattr(policy, 'local_only', False) is not True and not self.allow_external:
            raise BoundaryError('only_local_policies_enabled')
        charge = getattr(policy, 'decision_cost', 1)
        if charge not in (0, 1) or self.state['counts']['decisions'] + charge > self.task['budget']['planner_decisions']:
            self.block('decision_budget'); return
        self.state['counts']['decisions'] += charge
        self.state['trace'].append({'event': 'decision_call', 'model_calls': charge})
        started = self.clock()
        try:
            action = policy.decide(self.context(), timeout=min(15, self.remaining()))
            if self.remaining() <= 0 or self.clock() - started > 15:
                self.block('planner_timeout'); return
            self.state['trace'].append({'event': 'decision', 'action': copy.deepcopy(action)})
            self.execute(action)
        except BoundaryError as error:
            self.state['trace'].append({'event': 'rejected', 'reason': str(error)})
            if charge == 0:
                self.block('local_policy_invalid')
        except Exception:
            self.block('policy_error')

    def run(self, policy):
        # Hard bound also protects local policies that consume no model decisions.
        for _ in range(12):
            if self.state['phase'] != 'active': break
            self.step(policy)
        if self.state['phase'] == 'active': self.block('step_budget')
        return self.result()

    def result(self):
        return copy.deepcopy({'task_id': self.task['id'], 'status': self.state['phase'], 'stop_reason': self.state['stop_reason'],
                'selected': [{'id': eid, 'name': self.state['registry'][eid]['name'], 'fields': self.state['requirements'][eid]}
                             for eid in self.state['selected_ids']], 'evidence': self.state['evidence'],
                'counts': self.state['counts'], 'trace': self.state['trace'], 'remaining_ms': int(self.remaining() * 1000)})

    def checkpoint(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.temp_root) or path.exists():
            raise BoundaryError('checkpoint_path')
        if self.state['phase'] != 'active' or self.remaining() <= 0:
            raise BoundaryError('checkpoint_phase')
        payload = {'version': 'planning_checkpoint_v1', 'task_hash': fingerprint(self.task), 'manifest_hash': self.manifest_hash,
                   'source_hash': self.verifier.source_hash, 'state': self.state, 'evidence_hash': fingerprint(self.state['evidence']),
                   'remaining': self.remaining(), 'expires_at': self.expires_at}
        envelope = {'payload': payload, 'sha256': fingerprint(payload)}
        with path.open('x', encoding='utf-8') as stream: json.dump(envelope, stream, ensure_ascii=False, indent=2)

    @classmethod
    def restore(cls, path, task, tools, verifier, manifest_hash, temp_root, clock=time.monotonic, wall=time.time, allow_external=False):
        path = Path(path).resolve()
        if not path.is_relative_to(Path(temp_root).resolve()) or path.stat().st_size > 2_000_000:
            raise BoundaryError('checkpoint_path_or_size')
        try:
            envelope = json.loads(path.read_text(encoding='utf-8'))
            payload = envelope['payload']
            if envelope['sha256'] != fingerprint(payload) or payload['version'] != 'planning_checkpoint_v1':
                raise BoundaryError('checkpoint_integrity')
            if (payload['task_hash'] != fingerprint(task) or payload['manifest_hash'] != manifest_hash
                    or payload['source_hash'] != verifier.source_hash):
                raise BoundaryError('checkpoint_task_or_dataset')
            if any(type(payload[k]) not in (int, float) or not math.isfinite(payload[k]) for k in ('remaining', 'expires_at')):
                raise BoundaryError('checkpoint_time')
            remaining = min(payload['remaining'], payload['expires_at'] - wall())
            if not math.isfinite(remaining) or not 0 < remaining <= task['budget']['total_seconds']:
                raise BoundaryError('checkpoint_expired')
            restored = cls(task, tools, verifier, manifest_hash, temp_root, clock, wall, allow_external)
            saved = payload['state']
            if payload['evidence_hash'] != fingerprint(saved['evidence']):
                raise BoundaryError('checkpoint_evidence_hash')
            if (set(saved) != set(restored.state) or saved['phase'] != 'active' or saved['stop_reason'] != ''
                    or any(saved[k] != restored.state[k] for k in ('version','task_id','dataset_id','source_sha256'))
                    or len(saved['observations']) > 5 or len(saved['trace']) > 50):
                raise BoundaryError('checkpoint_state')
            # Rebuild all registries, evidence and requirements from validated observations.
            for observation in saved['observations']:
                action, result = observation['action'], observation['result']
                if result.get('dataset_id') != DATASET: raise BoundaryError('checkpoint_observation_dataset')
                restored.validate(action)
                if result['status'] == 'transient_error':
                    if action['kind'] != 'read_fields': raise BoundaryError('checkpoint_error')
                    for f in action['fields']:
                        restored.state['requirements'][action['entity_ids'][0]][f] = {'status': 'transient_error'}
                else:
                    restored.ingest(action, result)
                    restored.state['completed'].append(canonical(action))
            for key in ('registry', 'selected_ids', 'requirements', 'evidence', 'completed'):
                if restored.state[key] != saved[key]: raise BoundaryError('checkpoint_reference_or_state')
            read_events = [e for e in saved['trace'] if e['event'] == 'read']
            actual_attempts = {}
            for e in read_events:
                key = canonical(e['action']); actual_attempts[key] = actual_attempts.get(key, 0) + 1
            if actual_attempts != saved['attempts'] or any(x > 2 for x in actual_attempts.values()):
                raise BoundaryError('checkpoint_attempts')
            counts = saved['counts']
            decisions = sum(e['model_calls'] for e in saved['trace'] if e['event'] == 'decision_call')
            if any(type(v) is not int for v in counts.values()) or counts != {'reads': len(read_events), 'decisions': decisions, 'answers': 0}:
                raise BoundaryError('checkpoint_counts')
            if not 0 <= counts['reads'] <= task['budget']['read_calls'] or not 0 <= counts['decisions'] <= task['budget']['planner_decisions']:
                raise BoundaryError('checkpoint_budget')
            restored.state = copy.deepcopy(saved)
            restored.deadline = clock() + remaining
            restored.expires_at = min(payload['expires_at'], wall() + remaining)
            restored.state['trace'].append({'event': 'restored'})
            return restored
        except BoundaryError:
            raise
        except Exception as error:
            raise BoundaryError('checkpoint_invalid') from error
