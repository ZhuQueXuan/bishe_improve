"""Finite, source-checkable language rules; no account or model access."""
import re
from .user_memory_options import OPTIONS
from .requirements import RequirementState,changes_topic,requirements_only

EXCLUDED=re.compile(r'如果|假如|假设|比如|例如|引用|他说|她说|你说|妈妈|爸爸|家属|患者|不是我|[“”「」\"]')
HEALTH=re.compile(r'感冒|症状|病史|患|检查结果|用药|过敏|康复|发烧|发热|头痛|疼痛|糖尿病|高血压|住院')
STABLE=re.compile(r'以后|往后|今后|平时|默认|一直习惯')
TEMPORARY=re.compile(r'这次|本次|今天|这一问|这回')
FORGET=re.compile(r'忘记|别再记|不要再记|不再保存|清除.*记忆|别再按|不要再按')
KINDS={'style':re.compile(r'解释方式|表达偏好|通俗|术语|专业解释'),
       'length':re.compile(r'篇幅|长短|简短|详细|字数'),
       'background':re.compile(r'学习背景|护理|医学学习|学医')}


def preference_events(text):
    if EXCLUDED.search(text):return []
    events=[]
    for match in re.finditer(r'[^。！？!?；;，,\n]+',text):
        clause=match.group().strip()
        if not clause or EXCLUDED.search(clause) or HEALTH.search(clause):continue
        if FORGET.search(clause):
            kinds=list(OPTIONS) if re.search(r'所有.*(?:偏好|记忆)|全部.*(?:偏好|记忆)',clause) else [k for k,p in KINDS.items() if p.search(clause)]
            events.extend({'kind':k,'action':'forget','span':clause} for k in kinds)
            continue
        if TEMPORARY.search(clause):continue
        if re.search(r'不想|不需要|不喜欢|不要|不用|别用',clause) and not re.search(r'不要.*专业术语',clause):continue
        if STABLE.search(clause):
            values=[]
            if re.search(r'通俗|先解释.*术语|术语.*先解释|少用.*术语|不要.*专业术语',clause):values.append(('style','plain'))
            elif re.search(r'专业术语|专业解释',clause):values.append(('style','technical'))
            if re.search(r'简短|简洁|精简|一句话',clause):values.append(('length','brief'))
            elif re.search(r'详细|展开说明',clause):values.append(('length','detailed'))
            events.extend({'kind':k,'value':v,'action':'save','span':clause} for k,v in values)
        if re.search(r'我(?:正在|现在在|在)?学(?:习)?护理|我是护理(?:专业)?(?:学生|生)',clause):
            events.append({'kind':'background','value':'nursing','action':'save','span':clause})
        elif re.search(r'我(?:正在|现在在|在)?学医|我是医学(?:专业)?(?:学生|生)',clause):
            events.append({'kind':'background','value':'medicine','action':'save','span':clause})
    return events


def is_preference_only(text):
    if requirements_only(text):return True
    if TEMPORARY.search(text):return is_preference_only(TEMPORARY.sub('以后',text))
    remaining=text
    events=preference_events(text)
    for e in events:remaining=remaining.replace(e['span'],'')
    return bool(events) and (not re.sub(r'[。！？!?；;，,\s]','',remaining) or requirements_only(remaining))


def effective_presentation(items,history,query,guest=False,requirements=None):
    selected={m['kind']:dict(m) for m in items}
    if guest:
        for m in history:
            if m.get('role')!='user':continue
            for e in preference_events(m.get('content','')):
                if e['action']=='forget':selected.pop(e['kind'],None)
                else:selected[e['kind']]={'id':'session:'+e['kind'],'kind':e['kind'],'value':e['value'],'version':0,'scope':'session'}
    # Temporary/current phrasing only overrides this answer, never a stored row.
    for e in preference_events(query):
        if e['action']=='forget':selected.pop(e['kind'],None)
        else:selected[e['kind']]={'id':'current:'+e['kind'],'kind':e['kind'],'value':e['value'],'version':0,'scope':'current'}
    if requirements is None:
        requirements=RequirementState()
        for i,m in enumerate([*history,{'role':'user','content':query,'id':'current-question'}]):
            if m.get('role')!='user':continue
            requirements.next_turn()
            text=m.get('content','')
            if changes_topic(text):requirements.change_topic()
            requirements.apply(text,str(m.get('id',i)))
    for kind in ('style','length'):
        row=requirements.effective(kind)
        if row is None:continue
        if row['value'] is None:selected.pop(kind,None)
        else:selected[kind]={'id':row['scope']+':'+kind,'kind':kind,'value':row['value'],
            'version':0,'scope':row['scope'],'source_message_id':row['source_message_id'],
            'source_span':row['source_span']}
    return list(selected.values())
