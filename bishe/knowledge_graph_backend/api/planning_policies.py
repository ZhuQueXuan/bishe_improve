"""Local policies sharing the same harness; no real model calls or oracle imports."""
from .planning_runtime import FIELDS


def next_action(context):
    if not context['selected_ids']:
        return {'kind': 'read_relations', 'entity_id': context['seed_id'], 'relation': 'acompany_with'}
    pending = {}
    for eid, row in context['requirements'].items():
        fields = tuple(f for f in FIELDS if row[f]['status'] in ('pending', 'transient_error'))
        if fields: pending.setdefault(fields, []).append(eid)
    if pending:
        fields, ids = next(iter(pending.items()))
        return {'kind': 'read_fields', 'entity_ids': ids[:2], 'fields': list(fields)}
    statuses = [x['status'] for row in context['requirements'].values() for x in row.values()]
    return {'kind': 'stop', 'status': 'partial' if 'missing' in statuses else 'complete',
            'reason': '资料缺项' if 'missing' in statuses else '目标资料已覆盖', 'evidence_ids': context['evidence_ids']}


class RulePolicy:
    local_only = True
    decision_cost = 0
    def decide(self, context, timeout): return next_action(context)


class MockDynamicPolicy:
    """Contract stub, deliberately equivalent to rule policy. Not a learned planner."""
    local_only = True
    decision_cost = 1
    def __init__(self): self.inputs = []
    def decide(self, context, timeout):
        self.inputs.append(context)
        return next_action(context)


class SinglePlanPolicy:
    """One pre-authored symbolic plan, not a measured model-generated plan.

    The executor binds selected IDs from observations and follows a declared retry
    branch. Cursor is derived from shared task state for checkpoint continuation.
    """
    local_only = True
    def __init__(self, resumed=False):
        self.decision_cost = 0 if resumed else 1
        self.plan = (
            ('read_relations', 'seed', 'acompany_with'),
            ('read_fields', 'selected[0:2]', ('desc', 'prevent')),
            ('on_transient_error', 'retry_once', 'remaining_selected'),
            ('on_missing', 'keep_missing', 'continue'),
            ('stop', 'coverage_status', 'all_current_evidence'),
        )

    def decide(self, context, timeout):
        self.decision_cost = 0
        if not context['selected_ids']:
            return {'kind': self.plan[0][0], 'entity_id': context['seed_id'], 'relation': self.plan[0][2]}
        # Fixed branch: finish/retry unresolved fields, without new model planning.
        groups = {}
        for eid in context['selected_ids']:
            row = context['requirements'][eid]
            fields = tuple(f for f in self.plan[1][2] if row[f]['status'] in ('pending', 'transient_error'))
            if fields: groups.setdefault(fields, []).append(eid)
        if groups:
            fields, ids = next(iter(groups.items()))
            return {'kind': self.plan[1][0], 'entity_ids': ids[:2], 'fields': list(fields)}
        missing = any(x['status'] == 'missing' for row in context['requirements'].values() for x in row.values())
        return {'kind': 'stop', 'status': 'partial' if missing else 'complete',
                'reason': '固定计划执行完成', 'evidence_ids': context['evidence_ids']}
