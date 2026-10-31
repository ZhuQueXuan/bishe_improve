"""Finite task bindings and minimal usage receipts; no models, DB writes or prices."""
import copy
from .planning_runtime import fingerprint

VERSION = 'task-memory-v1'
ZERO = {'reads': 0, 'decisions': 0, 'answers': 0}


def presentation_items(items):
    return [{k: row[k] for k in ('id','version','kind','value','scope','source_message_id','source_span') if k in row}
            for row in items]


def memory_binding(health, preferences, presentation, profile_version=0, conversation_version=0, input_chain=None):
    return {'version': VERSION, 'profile_version': profile_version, 'conversation_version': conversation_version,
            'input_chain':input_chain,
            'health': [{'id': r['id'], 'version': r['version'], 'source_hash': fingerprint(r)} for r in health],
            'preferences': presentation_items(preferences), 'presentation': presentation_items(presentation)}


def receipt(task_id, sequence, role, record=None):
    record = record if isinstance(record, dict) else {}
    usage = record.get('usage')
    keys = ('prompt_tokens','completion_tokens','total_tokens')
    if (not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in keys)
            or usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']):
        usage = None
    else:
        usage = {k: usage[k] for k in keys}
    return {'task_id': task_id, 'sequence': sequence, 'role': role, 'usage': usage,
            'usage_status': 'reported' if usage is not None else 'unknown', 'cost': None,
            'cost_status': 'not_calculated'}


def task_audit(harness, binding, receipts, before, resumed, status):
    counts = copy.deepcopy(harness.state['counts'])
    pending = [{'entity_id': eid, 'field': field, 'status': value['status']}
               for eid, fields in harness.state['requirements'].items() for field, value in fields.items()
               if value['status'] in {'pending','transient_error'}]
    return {'version': VERSION, 'task_id': harness.task['id'], 'status': status, 'resumed': resumed,
            'selected_memory': {'profile_version': binding['profile_version'],
                'conversation_version': binding['conversation_version'],
                'health': [{k:r[k] for k in ('id','version')} for r in binding['health']],
                'presentation': [{k:r[k] for k in ('id','version','kind','scope') if k in r} for r in binding['presentation']]},
            'evidence_ids': list(harness.state['evidence']), 'pending': pending,
            'counts': {'cumulative': counts, 'before_request': copy.deepcopy(before),
                       'this_request': {k: counts[k]-before[k] for k in ZERO}},
            'limits': copy.deepcopy(harness.task['budget']), 'remaining_ms': int(harness.remaining()*1000),
            'receipts': copy.deepcopy(receipts), 'cost': None, 'cost_status': 'not_calculated',
            'scope': 'source_and_call_accounting_not_medical_or_model_quality'}
