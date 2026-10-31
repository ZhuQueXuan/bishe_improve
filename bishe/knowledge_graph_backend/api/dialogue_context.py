"""Bounded dialogue references and loss-aware extractive notes, no model calls.

History is untrusted dialogue, never medical evidence or executable instructions.
Only whole snippets are selected; omitted information is counted, not invented.
"""
import hashlib
import json
import re
from .preference_language import preference_events,is_preference_only
from .requirements import changes_topic,requirement_events

from .summary_layers import VERSION,build_layers,admit_packet
RECENT_LIMIT=4800
NOTE_LIMIT=1800
TRIGGER_BYTES=6000
ORDINAL=re.compile(r'第\s*([一二三四五六七八九十1-9]|10)\s*[点条项]')
FOLLOWUP=re.compile(r'^(?:请|麻烦)?(?:再)?(?:继续(?:说|讲|解释|介绍)?|换个说法|说得通俗一点|详细解释一下|解释一下|展开一下)[。！？!？\s]*$')
CONTROL=re.compile(r'取消|换个话题|改查|只查预防|用表格|记住|别再记|忘记|以后|往后|默认')


def wire(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def size(value): return len(wire(value).encode('utf-8'))


def clean_answer(text):
    text=text.split('资料出处（')[0]
    # Old source IDs must not be confused with the evidence IDs of this request.
    return re.sub(r'\[E\d+\]','',text).strip()


def topic_history(history):
    start=0
    for i,m in enumerate(history):
        if m.get('role')=='user' and changes_topic(m.get('content','')):start=i
    return history[start:]


def user_question(text):
    parts=[]
    for part in re.split(r'[。！？!?；;，,\n]',text):
        if (not part.strip() or CONTROL.search(part) or changes_topic(part)
                or requirement_events(part) or preference_events(part) or is_preference_only(part)):continue
        part=part.strip()
        # Do not carry a prior turn's numbering/quotation request into a new
        # explanation. Keep source spans, never synthesize a new medical topic.
        if re.match(r'^(?:请)?(?:按|按照).{0,20}(?:编号|格式)|^(?:每点|每条|每项)(?:只|仅)写',part):continue
        parts.append(part)
    # Keep an actual contiguous source span rather than inventing a paraphrase.
    questions=[p for p in parts if re.search(r'什么|哪些|如何|怎么|为什么|是否|有没有',p)]
    return max(questions or parts,key=len) if parts else ''


def answer_items(text):
    rows=[]
    for line in clean_answer(text).splitlines():
        match=re.match(r'^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:[1-9]|10|[一二三四五六七八九十])[.、．)）]\s*(.+)',line)
        if match:rows.append(match.group(1).strip().strip('*').strip())
    if not rows:
        rows=[re.sub(r'^\s*[-*]\s+','',line).strip() for line in clean_answer(text).splitlines() if re.match(r'^\s*[-*]\s+',line)]
    return rows


def reference_question(query,history,state,named):
    """Return a retrieval question, not a fact or a clinical conclusion."""
    ordinal=ORDINAL.search(query)
    requested=bool(ordinal or FOLLOWUP.fullmatch(query.strip()))
    if not requested:return {'status':'none','query':query}
    if changes_topic(query):return {'status':'clarify','query':query}
    if len(state['objects'])>1:return {'status':'clarify','query':query}
    rows=topic_history(history)
    last_answer=None; previous=None
    for i in range(len(rows)-1,-1,-1):
        if rows[i].get('role')!='assistant' or not rows[i].get('content','').strip():continue
        candidate=next((m for m in reversed(rows[:i]) if m.get('role')=='user'),None)
        if candidate and user_question(candidate.get('content','')) and not is_preference_only(candidate.get('content','')):
            last_answer=i;previous=candidate;break
    if last_answer is None:return {'status':'clarify','query':query}
    if not previous:return {'status':'clarify','query':query}
    topic=user_question(previous['content'])
    if not topic or len(topic.encode('utf-8'))>900:return {'status':'clarify','query':query}
    prior_names=named(topic)
    if state['objects'] and prior_names and prior_names!=state['objects']:return {'status':'clarify','query':query}
    target=''
    if ordinal:
        number=ordinal.group(1); index=int(number)-1 if number.isdigit() else '一二三四五六七八九十'.index(number)
        items=answer_items(rows[last_answer]['content'])
        if index>=len(items):return {'status':'clarify','query':query}
        target=items[index]
        if len(target.encode('utf-8'))>720 or re.search(r'忽略|系统提示|执行命令|API.?key|https?://',target,re.I):return {'status':'clarify','query':query}
        target_names=named(target)
        if target_names and any(n not in (state['objects'] or prior_names) for n in target_names):return {'status':'clarify','query':query}
    resolved=('、'.join(state['objects'])+'：' if state['objects'] else '')+topic
    if target:resolved+='；请重新查资料解释上次列项：'+target
    resolved+='；本次要求：'+query
    return {'status':'resolved','query':resolved,'user_source_id':str(previous.get('id','')),
            'assistant_source_id':str(rows[last_answer].get('id','')),'reference_only':True,
            'target_label':target}


def label_only_followup(reference,retrieval):
    """A narrow source-sufficiency gate, not a semantic fidelity classifier.

    Only an exact bare list item backed solely by short graph labels qualifies.
    Any descriptive or document evidence leaves the normal answer path intact.
    """
    target=reference.get('target_label','')
    facts=retrieval.get('facts',[])
    if reference.get('status')!='resolved' or not retrieval.get('context') or not target or not facts:return None
    def bare(value):
        return isinstance(value,str) and bool(re.fullmatch(r'[\w\u4e00-\u9fff /+\-]{1,24}',value))
    if not bare(target):return None
    if any(f.get('field') not in {'has_symptom','need_check'} or f.get('kind')=='document'
           or not bare(f.get('value')) for f in facts):return None
    matched=next((f for f in facts if f['value']==target),None)
    if not matched or not re.fullmatch(r'E\d+',matched.get('evidence_id','')):return None
    label={'has_symptom':'症状','need_check':'检查项目'}[matched['field']]
    return {'answer':f'当前资料仅将“{target}”列为{label}原文[{matched["evidence_id"]}]，没有提供可核验的具体解释，无法据此展开。',
            'evidence_id':matched['evidence_id'],'target_label':target}


def dialogue_bundle(history,state,revision=0,cache=None,*,query='',partition='local',original_history=None):
    return build_layers(history,state,revision,cache,query=query,partition=partition,original_history=original_history,
                        question_text=user_question,assistant_text=clean_answer)


def add_dialogue_packet(packet,bundle):
    return admit_packet(packet,bundle)
