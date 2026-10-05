"""Sourced explicit-phrase session memory and bounded context components.

No LLM/persistence/account access here. Formal ChatMemory reuses these primitives;
the frozen window evaluation still uses the same narrow extractor for scoring.
"""
import copy
import json
import re
from .planning_runtime import BoundaryError


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def measured(payload):
    size = len(payload.encode('utf-8'))
    return {'input_bytes': size, 'estimated_tokens': (size + 2) // 3,
            'token_estimate_method': 'ceil(UTF-8 bytes / 3), not exact provider tokens',
            'actual_usage': None}


class SessionMemory:
    def __init__(self, session_id, catalog):
        if not isinstance(session_id, str) or not session_id or not catalog:
            raise BoundaryError('session_configuration')
        if any(not isinstance(x, str) or not x or len(x)>120 for x in catalog):
            raise BoundaryError('session_catalog')
        self.session_id = session_id
        self.catalog = tuple(sorted(set(catalog), key=lambda x: (-len(x), x)))
        self.entries, self.messages, self.audit = {}, {}, []
        self.topic = 1
        self.version = 0

    def _set(self, key, value, source):
        old = self.entries.get(key)
        self.version += 1
        if old:
            self.audit.append(dict(copy.deepcopy(old), key=key, active=False,
                                   superseded_by=self.version))
        self.entries[key] = {'value': copy.deepcopy(value), 'source_message_id': source,
                             'version': self.version, 'topic': self.topic,
                             'active': value is not None}

    def ingest(self, message):
        if not isinstance(message, dict): raise BoundaryError('session_message')
        if message.get('session_id') != self.session_id: return False
        if (not isinstance(message.get('id'), str) or not 0<len(message['id'])<=100
                or message.get('role') not in ('user','assistant')
                or not isinstance(message.get('content'), str)
                or len(message['content'].encode('utf-8'))>16000):
            raise BoundaryError('session_message')
        mid=message['id']
        if mid in self.messages:
            if self.messages[mid] != message: raise BoundaryError('message_revision_requires_new_id')
            return False
        self.messages[mid] = copy.deepcopy(message)
        if message['role'] != 'user': return True
        text=message['content']
        if '换个话题' in text:
            self.topic += 1
            for key in ('objects','field','format'): self._set(key, None, mid)
        # Only an explicit discussion/correction clause may introduce objects.
        clause=re.search(r'(?:讨论|改查)([^。！？\n]+)', text)
        if clause:
            fragment=clause.group(1)
            names=[]
            for name in self.catalog:
                if name in fragment:
                    names.append(name)
                    fragment=fragment.replace(name,'')
            self._set('objects', sorted(names), mid)
        if '取消只查预防' in text: self._set('field', None, mid)
        elif '只查预防' in text: self._set('field', 'prevent', mid)
        if '取消表格' in text: self._set('format', None, mid)
        elif '用表格' in text: self._set('format', 'table', mid)
        return True

    def snapshot(self):
        def value(key): return copy.deepcopy(self.entries.get(key, {}).get('value'))
        objects=value('objects') or []
        return {'status': 'ready' if len(objects)==1 else 'clarify',
                'objects': objects, 'field': value('field'), 'format': value('format')}

    def state_packet(self):
        # Tombstones retained: cancellation is meaningful even though inactive.
        return {'topic': self.topic, 'version': self.version,
                'entries': copy.deepcopy(self.entries)}


POLICIES = '仅整理资料；讨论对象不是用户病历；状态有来源，更正和取消优先；不确定对象需澄清。'


def build_context(memory, current_id, budget_bytes=4096):
    if type(budget_bytes) is not int or not 128<=budget_bytes<=12000:
        raise BoundaryError('context_budget')
    current=memory.messages.get(current_id)
    if not current or current['role']!='user': raise BoundaryError('current_user_message')
    base={'policies': POLICIES, 'task': {'id':current_id,'question':current['content']},
          'state': memory.state_packet()}
    for entry in memory.entries.values():
        source=memory.messages.get(entry['source_message_id'])
        if not source or source['role']!='user' or source['session_id']!=memory.session_id:
            raise BoundaryError('state_source')
    payload=wire(base)
    if len(payload.encode('utf-8'))>budget_bytes:
        # No partial mandatory context is passed to a model.
        return dict(status='context_limit', payload=None, available_bytes=budget_bytes,
                    mandatory_bytes=len(payload.encode('utf-8')), input_bytes=0,
                    actual_usage=None, selection=[])
    # References already preserve the latest source for each slot. Do not append
    # original control messages: they can contain superseded constraints in other
    # slots (e.g. a still-valid object and a subsequently cancelled format).
    selected=sorted({e['source_message_id'] for e in memory.entries.values()})
    payload=wire(base)
    return dict(status=memory.snapshot()['status'], payload=payload,
                selection=selected, **measured(payload))


def window_context(messages, session_id, catalog, budget_bytes):
    """Window information-availability surrogate, not the production API.

    Same source filtering, grammar and byte budget as the candidate. Oldest
    messages are discarded whole to fit; no transcript is silently cut mid-text.
    """
    selected=[m for m in messages if m.get('session_id')==session_id][-10:]
    if not selected or selected[-1]['role']!='user': raise BoundaryError('current_user_message')
    task={'id':selected[-1]['id'],'question':selected[-1]['content']}
    while selected:
        payload=wire({'policies':POLICIES,'task':task,'history':selected})
        if len(payload.encode('utf-8'))<=budget_bytes: break
        if len(selected)==1:
            return {'status':'context_limit','payload':None,'input_bytes':0,'actual_usage':None},None
        selected=selected[1:]
    state=SessionMemory(session_id,catalog)
    for m in selected: state.ingest(m)
    return dict(status=state.snapshot()['status'],payload=payload,
                selection=[m['id'] for m in selected],**measured(payload)),state.snapshot()
