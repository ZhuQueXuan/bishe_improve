"""Finite explicit instruction scopes shared by production state and presentation.

No model, database or health fact extraction. Old experimental SessionMemory
keeps its original grammar; production ChatMemory opts into this state machine.
"""
import copy
import re

CLAUSES = re.compile(r'[^。！？!?；;，,\n]+')
EXCLUDED = re.compile(r'如果|假如|假设|比如|例如|引用|他说|她说|你说|妈妈说|爸爸说|[“”「」"]')
CURRENT = re.compile(r'这次(?!会话|对话)|本次(?!会话|对话)|这一问|这回|今天|当前回答|这个回答')
SESSION = re.compile(r'接下来|后面|本(?:次)?(?:会话|对话)|当前会话|这(?:个|次|段)?(?:对话|会话)|从现在起|一直')
TOPIC = re.compile(r'当前话题|这个话题|本话题|这个病期间')
FUTURE = re.compile(r'以后|往后|今后|平时|默认|一直习惯')
SWITCH = re.compile(r'^(?:(?:我们|咱们|现在|请|接下来)\s*){0,3}'
                    r'(?:换(?:一)?个(?:话题|主题)|聊(?:一)?点别的|说(?:一)?点别的)')
UNDO = re.compile(r'^(?:请)?(?:取消|撤销|收回)(?:我)?(?:刚才|上次|上一条|上一个)的?(?:要求|设置|指令)$')
FORMAT_OFF = re.compile(r'(?:取消|不要再?|不用|别再?用|不再(?:用|使用)|不使用)(?:用|使用)?表格(?:形式|要求)?')
FORMAT_ON = re.compile(r'(?:用|使用|采用)表格(?:形式)?')
FIELD_OFF = re.compile(r'(?:取消|不再|不要|不用)(?:只查|只看|只说|只讲|仅查|仅看)预防(?:的要求|要求)?')
FIELD_ON = re.compile(r'(?:只查|只看|只说|只讲|仅查|仅看)预防')
FORGET = re.compile(r'忘记|别再记|不要再记|不再保存|清除.*记忆|别再按|不要再按')


def changes_topic(text):
    return not EXCLUDED.search(text) and any(SWITCH.match(m.group().strip()) for m in CLAUSES.finditer(text))


def scope_of(clause, fallback=None):
    if CURRENT.search(clause): return 'current'
    if TOPIC.search(clause): return 'topic'
    if SESSION.search(clause) and '一直习惯' not in clause: return 'session'
    if FUTURE.search(clause): return 'long_term'
    return fallback


def requirement_events(text):
    if EXCLUDED.search(text): return []
    events = []
    inherited = None
    for match in CLAUSES.finditer(text):
        clause = match.group().strip()
        if UNDO.fullmatch(clause):
            events.append({'action':'undo', 'span':clause})
            continue
        scope = scope_of(clause, inherited)
        if scope: inherited = scope
        candidates = []
        for key, off, on, value in [('format',FORMAT_OFF,FORMAT_ON,'table'),
                                     ('field',FIELD_OFF,FIELD_ON,'prevent')]:
            found = off.search(clause)
            if found: candidates.append((key,None,found.group(), 'cancel'))
            else:
                found = on.search(clause)
                if found: candidates.append((key,value,found.group(),'set'))
        if FORGET.search(clause) or re.search(r'取消(?:篇幅|长短|解释方式|表达偏好)',clause):
            for key, pattern in [('length',r'篇幅|长短|简短|详细|字数'),('style',r'解释方式|表达偏好|通俗|术语')]:
                if re.search(pattern,clause): candidates.append((key,None,clause,'cancel'))
        elif not re.search(r'不想|不需要|不喜欢|不要|不用|别用',clause) or re.search(r'不要.*专业术语',clause):
            for key, options in [
                ('length',[('brief',r'简短|简洁|精简|一句话'),('detailed',r'详细|展开说明')]),
                ('style',[('plain',r'通俗|先解释.*术语|术语.*先解释|少用.*术语|不要.*专业术语'),
                          ('technical',r'专业术语|专业解释')])]:
                for value, pattern in options:
                    found = re.search(pattern,clause)
                    if found:
                        candidates.append((key,value,found.group(),'set'))
                        break
        for key, value, span, action in candidates:
            effective_scope = scope or ('topic' if key in {'field','format'} else 'current')
            # Format/field have no approved account-wide storage. Explicit
            # future wording keeps them within this conversation.
            if effective_scope == 'long_term' and key in {'field','format'}:
                effective_scope = 'session'
            if action == 'cancel' and not scope: effective_scope = 'all'
            events.append({'key':key,'value':value,'action':action,
                           'scope':effective_scope,'span':span})
    return events


def requirements_only(text):
    """Acknowledge a small complete command grammar, never consume a question."""
    if re.fullmatch(r'(?:请|麻烦)?(?:再)?(?:详细解释一下|解释一下|展开一下|换个说法|说得通俗一点)[。！？!?\s]*',text.strip()):
        return False  # These are substantive follow-ups, not settings commands.
    if not requirement_events(text): return False
    for match in CLAUSES.finditer(text):
        clause = match.group().strip()
        if UNDO.fullmatch(clause): continue
        clause = CURRENT.sub('',TOPIC.sub('',SESSION.sub('',FUTURE.sub('',clause))))
        clause = re.sub(r'^(?:请|都|要|回答|解释|给我|的要求|里|中|内|期间|就|再)+','',clause)
        clause = re.sub(r'^(?:用|使用|采用)(?=专业术语|专业解释|通俗)','',clause)
        clause = re.sub(r'(?:即可|就好|一点|一下|一些|点|吧|回答|解释|说明|显示|展示|的要求|要求|都|更)$','',clause)
        for pattern in (FORMAT_OFF,FORMAT_ON,FIELD_OFF,FIELD_ON): clause = pattern.sub('',clause)
        clause = re.sub(r'取消(?:篇幅|长短|解释方式|表达偏好)(?:的要求|要求)?','',clause)
        clause = re.sub(r'通俗(?:解释)?|简短|简洁|精简|一句话|详细|展开说明|术语先解释(?:一下)?|先解释(?:一下)?术语|专业术语|专业解释','',clause)
        clause = re.sub(r'^(?:请|都|要)|(?:回答|解释|说明|点|一点|一下|吧|都)$','',clause)
        if clause.strip(): return False
    return True


class RequirementState:
    KEYS = ('field','format','style','length')

    def __init__(self):
        self.layers = {k:{} for k in self.KEYS}
        self.turn, self.topic, self.order = 0, 1, 0
        self.last_group = None
        self.last_action = None

    def next_turn(self):
        self.turn += 1
        changed = []
        for key, layers in self.layers.items():
            if layers.pop('current',None) is not None: changed.append(key)
        self.last_action = None
        return changed

    def change_topic(self):
        self.topic += 1
        changed = []
        for key, layers in self.layers.items():
            if layers.pop('topic',None) is not None: changed.append(key)
        self.last_group = None
        return changed

    def effective(self, key):
        layers = self.layers[key]
        if 'current' in layers: return copy.deepcopy(layers['current'])
        lasting = [layers[s] for s in ('topic','session') if s in layers]
        if lasting: return copy.deepcopy(max(lasting,key=lambda row:row['order']))
        return None

    def packet(self):
        return {key:row for key in self.KEYS if (row:=self.effective(key)) is not None}

    def apply(self, text, source):
        events = requirement_events(text)
        changed = set()
        before = copy.deepcopy(self.layers)
        for event in events:
            if event['action'] == 'undo':
                group = self.last_group
                if not group or group.get('barrier'):
                    self.last_action = 'long_term_needs_explicit_change' if group else 'nothing_to_undo'
                    continue
                for key in group['keys']:
                    restored = copy.deepcopy(group['before'][key])
                    restored.pop('current',None)  # Never revive an expired turn.
                    if group['topic'] != self.topic: restored.pop('topic',None)
                    self.layers[key] = restored
                    changed.add(key)
                self.last_group = None
                self.last_action = 'undone'
                before = copy.deepcopy(self.layers)
                continue
            key, scope = event['key'], event['scope']
            self.order += 1
            if scope == 'long_term':
                # SQLite owns long-term changes and its switches/revisions.
                # A vague undo must not roll back an older, unrelated command.
                self.last_group = {'barrier':True}
                self.last_action = 'long_term'
                # An explicit new default applies now, even with collection off.
                # Its persistence remains the responsibility of automatic_memory.
                self.layers[key] = {'current':{'value':event['value'],'scope':'current',
                    'source_message_id':source,'source_span':event['span'],
                    'topic':self.topic,'turn':self.turn,'order':self.order}}
                changed.add(key)
                continue
            if scope == 'all': self.layers[key] = {}
            else:
                self.layers[key][scope] = {'value':event['value'],'scope':scope,
                    'source_message_id':source,'source_span':event['span'],
                    'topic':self.topic,'turn':self.turn,'order':self.order}
            changed.add(key)
        regular = {e['key'] for e in events if e['action'] != 'undo' and e['scope'] != 'long_term'}
        if regular:
            self.last_group = {'before':before,'keys':regular,'topic':self.topic}
        if any(e.get('scope')=='long_term' for e in events):self.last_group={'barrier':True}
        return changed
