"""D: deterministic, bounded personal retrieval. No storage writes or networks.

Exact disease names plus finite time/state/reference keywords. Relative periods
are anchored to their own source dates, in the application's Chinese timezone.
Calendar interpretation is retrieval metadata, never a diagnosis or stored edit.
"""
import re
from datetime import datetime,date,timedelta,timezone

VERSION='personal-selection-v1'
CHINA=timezone(timedelta(hours=8))
MAX_ROWS=5
PERIOD=re.compile(r'\d{4}年(?:\d{1,2}月)?(?:\d{1,2}日)?|(?:去年|前年|今年)(?:\d{1,2}月)?(?:\d{1,2}日)?|上个月|这个月|本月|上周|本周|这周|昨天|今天')
RECALL=re.compile(r'记得我|记住了我|我.{0,10}(?:保存|记录|提到|说过).{0,15}(?:什么|哪些|什么时候|何时|是否|吗|情况)|我.{0,6}(?:过去|以前|之前).{0,8}(?:说过|提过|保存|记录).{0,8}(?:什么|哪些|什么时候|情况)')
MEDICAL=re.compile(r'预防|治疗|症状|检查|用药|饮食|建议|资料|原因|怎么|如何|注意|诊断')
PREFERENCE_RECALL=re.compile(r'偏好|解释方式|回答|篇幅|简短|详细|通俗|学习背景|学医|护理|专业术语')
HEALTH_RECALL=re.compile(r'疾病|病史|健康|情况|经历|已经好|已康复|康复了|否认|不确定|疑似')
FAMILY=re.compile(r'我(?:的)?(?:妈妈|母亲|爸爸|父亲|妻子|丈夫|孩子|儿子|女儿|朋友|同事|家属)|他|她|家属|父母')
PERSONAL=re.compile(r'我的|本人|根据我|对我|结合我|我(?:上个月|上周|去年|前年|昨天|今天|现在|以前|\d{4}年)')
REFERENCE=re.compile(r'我.{0,14}(?:记住|保存).{0,6}(?:那个病|那种病|这个病|这种病)|我(?:的)?(?:这|那)(?:个|种)?病')
EXCLUDED=re.compile(r'如果|假如|假设|比如|例如|引用|他说|她说|[“”「」"]')
STATE_WORDS=[('resolved',r'已经好|已康复|康复了|已结束|痊愈'),('denied',r'否认|没有患|没得'),
             ('uncertain',r'不确定|疑似|可能'),('current',r'现在|目前|当前')]

def is_recall_query(query):
    return bool(RECALL.search(query) and not MEDICAL.search(query))

def stamp(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('aware timestamp required')
    return parsed.astimezone(CHINA)

def interval(raw,anchor):
    """Half-open calendar periods for the supported literal grammar only."""
    if not raw:return None
    d=stamp(anchor).date();year=d.year;month=None;day=None
    absolute=re.fullmatch(r'(\d{4})年(?:(\d{1,2})月)?(?:(\d{1,2})日)?',raw)
    relative=re.fullmatch(r'(去年|前年|今年)(?:(\d{1,2})月)?(?:(\d{1,2})日)?',raw)
    if absolute or relative:
        m=absolute or relative
        year=int(m[1]) if absolute else year+{'去年':-1,'前年':-2,'今年':0}[m[1]]
        month=int(m[2]) if m[2] else None;day=int(m[3]) if m[3] else None
    elif raw in {'上周','本周','这周'}:
        start=d-timedelta(days=d.weekday()+(7 if raw=='上周' else 0))
        return start,start+timedelta(days=7)
    elif raw in {'昨天','今天'}:
        start=d-timedelta(days=raw=='昨天');return start,start+timedelta(days=1)
    elif raw in {'上个月','这个月','本月'}:
        month=d.month
        if raw=='上个月':year,month=(year-1,12) if month==1 else (year,month-1)
    else:return None
    if day is not None:
        start=date(year,month or 1,day);return start,start+timedelta(days=1)
    if month is not None:
        return date(year,month,1),date(year+1,1,1) if month==12 else date(year,month+1,1)
    return date(year,1,1),date(year+1,1,1)

def period_query(query,now):
    matches=list(PERIOD.finditer(query))
    if len(matches)>1:return {'raw':None,'interval':None,'invalid':True}
    raw=matches[0].group() if matches else None
    try:return {'raw':raw,'interval':interval(raw,now) if raw else None,'invalid':False}
    except (ValueError,OverflowError):return {'raw':raw,'interval':None,'invalid':True}

def event_period(item):
    r=item['report'];reference=item.get('event_time_reference')
    return interval(reference['raw'],reference['recorded_at']) if reference else interval(r['event_time']['raw'],r['recorded_at'])

def personal_plan(query,rows,health,names,now,*,enabled=True,authenticated=True):
    """rows are owner-filtered active, source-verified records supplied by adapter."""
    recall=is_recall_query(query)
    plan={'version':VERSION,'rows':[],'clarification':None,'local_answer':None,'recall':recall,
          'audit':{'method':'structure_and_finite_keywords','selected':[],'excluded':[],
                   'timezone':'Asia/Shanghai (+08:00)','time_interpretation':'source-anchored calendar period; raw text unchanged'}}
    audit=plan['audit']
    def stop(reason,text=None):
        audit['status']=reason
        if text:plan['clarification']=text
        return plan
    if not authenticated:
        if recall:plan['local_answer']='登录后才能读取已确认保存的长期自述；游客历史不会作为账号长期记忆读取。'
        return stop('guest')
    if not enabled:
        if recall:plan['local_answer']='长期记忆的使用开关尚未打开，本次没有读取已保存自述。'
        return stop('usage_disabled')
    if FAMILY.search(query):
        if recall:plan['local_answer']='本版尚未保存家属的长期健康自述，不能用本人的记录代替。'
        return stop('family_not_supported')
    if EXCLUDED.search(query):return stop('hypothetical_or_quoted')
    if recall and not names and not HEALTH_RECALL.search(query):
        return stop('recall_type_unclear','请说明希望回顾已保存的疾病自述，还是表达偏好；本次没有选入个人记录。')
    current=[r for r in health.records if r['id'] in health.current['report_ids'] and r['subject']=='self']
    personal=recall or bool(current or PERSONAL.search(query) or REFERENCE.search(query))
    referential=bool(re.search(r'它|这种病|该病|这个病',query))
    if referential and health.current.get('subjects')==['self']:personal=True
    if not personal:return stop('not_personal')
    period=period_query(query,now);audit['requested_period']=period['raw']
    if period['invalid']:return stop('time_unclear','请只指定一个有效的事件时间范围，避免把不同时间的情况合在一起。')
    state=next((s for s,p in STATE_WORDS if re.search(p,query)),None)
    audit['state_filter']=state
    requested=list(names)
    if not requested and referential and health.current.get('subjects')==['self']:
        requested=list(getattr(health,'discussed_names',[]))
        if len(requested)>1:return stop('subject_unclear','当前涉及多个疾病，请说明希望参考哪一项本人记录。')
        if requested:audit['reference']='current_conversation_self_topic'
    if not requested and REFERENCE.search(query):
        values=sorted({m['report']['value'] for m in rows})
        if len(values)!=1:return stop('reference_unclear','已保存的情况不能唯一确定，请补充具体疾病名称和事件时间。')
        requested=values;audit['reference']='explicit_saved_reference_unique_disease'
    if not requested and not recall:return stop('no_specific_personal_target')
    audit['requested_names']=requested
    candidates=[];unclear_period=False
    for item in rows:
        r=item['report'];reason=None;basis='exact_disease' if requested else 'explicit_memory_recall'
        if r.get('subject')!='self':reason='unsupported_subject'
        elif requested and r['value'] not in requested:reason='different_disease'
        if reason is None and period['interval']:
            try:old_period=event_period(item)
            except (ValueError,OverflowError):old_period=None
            wanted=period['interval']
            if old_period and wanted[0]<=old_period[0] and old_period[1]<=wanted[1]:basis+=' + source_anchored_period'
            elif old_period and old_period[0]<wanted[1] and wanted[0]<old_period[1]:
                reason='event_period_too_coarse';unclear_period=True
            else:reason='different_or_unknown_event_period'
        if reason is None and state:
            if r['event_state']!=state:reason='different_state'
            else:basis+=' + explicit_state'
        if reason:
            audit['excluded'].append({'id':item['id'],'version':item['version'],'reason':reason});continue
        reports=[c for c in current if c['attribute']==r['attribute']]
        # A historical statement or clear new episode is not automatically a
        # correction to a current saved self-report.
        conflict=any(c['event_state']!=r['event_state'] and c['event_state']!='past' and r['event_state']!='past'
                     and not PERIOD.search(c['event_time']['raw'] or '')
                     and not re.search(r'又|再次|另一次',query) for c in reports)
        if conflict:return stop('conflict','你这次的自述与已保存的同名记录状态不同。请说明是否更正原记录，还是另一次情况；尚未自动修改长期记忆。可在记忆设置核对后更新。')
        in_session=[c for c in health.effective.values() if c['subject']=='self' and c['attribute']==r['attribute']]
        if not recall and period['interval']:
            same_period=[]
            for c in in_session:
                try:p=interval(c['event_time']['raw'],c['recorded_at'])
                except (ValueError,OverflowError):p=None
                if p and period['interval'][0]<=p[0] and p[1]<=period['interval'][1]:same_period.append(c)
            if same_period:
                if any(c['event_state']!=r['event_state'] for c in same_period):
                    return stop('session_event_unclear','会话与长期记录在同一时段有同名但状态不同的自述，请确认是否同一次事件。')
                audit['excluded'].append({'id':item['id'],'version':item['version'],'reason':'current_session_same_period_precedence'});continue
        # Only let the session displace the same requested temporal scope.
        if not recall and (reports or in_session) and not period['interval']:
            audit['excluded'].append({'id':item['id'],'version':item['version'],'reason':'current_session_precedence'});continue
        candidates.append((item,basis))
    if unclear_period:return stop('time_precision_unclear','保存的事件时间比这次问题更宽，无法确认是否同一段时间。请放宽时间范围或先核对原记录。')
    # A model-facing background question must identify an episode. A local
    # explicit recall may list several independently labelled episodes.
    attrs=[m['report']['attribute'] for m,_ in candidates]
    if not recall and len(attrs)!=len(set(attrs)):
        return stop('episode_unclear','已保存多次同名疾病自述，请明确要参考哪次的时间；不会按记录新旧替你选择。可在记忆设置核对。')
    if len(candidates)>MAX_ROWS:
        return stop('too_many_relevant','相关记忆较多，请指定疾病、时间或状态后再查询；本次没有截取部分记录代替全部。')
    # Relevance constraints are above; date is a deterministic display tiebreak
    # only. No importance/recency score overrides status or event association.
    candidates.sort(key=lambda x:(x[0]['report']['value'],x[0]['report']['recorded_at'],x[0]['id']))
    plan['rows']=[m for m,_ in candidates]
    if candidates and audit.get('reference')=='explicit_saved_reference_unique_disease' and not recall:
        plan['resolved_query']=requested[0]+'：'+query
        audit['resolved_query']=plan['resolved_query']
    audit['selected']=[{'id':m['id'],'version':m['version'],'reason':basis} for m,basis in candidates]
    audit['status']='selected' if candidates else 'no_match'
    if recall:plan['local_answer']=recall_answer(plan['rows'])
    return plan

def recall_answer(rows):
    if not rows:return '没有找到符合这次条件的、正在启用且来源可核验的已确认自述；这不代表你没有相关疾病或经历。'
    labels={'current':'当时自述存在','past':'过去经历','resolved':'用户报告已结束','denied':'当时否认','uncertain':'当时尚不确定'}
    lines=['以下是你确认保存的自述，仅作回顾，不能据此判断你今天是否患病：']
    for item in rows:
        r=item['report'];recorded=stamp(r['recorded_at']).strftime('%Y-%m-%d %H:%M +08:00')
        ref=item.get('event_time_reference')
        event_raw=r['event_time']['raw'] or (ref['raw'] if ref else '未说明')
        supporting=f"；事件时间沿用已确认同一事件的旧来源：{ref['source']['text']}，记录于{stamp(ref['recorded_at']).strftime('%Y-%m-%d %H:%M +08:00')}" if ref else ''
        lines.append(f"- {r['value']}；{labels[r['event_state']]}；事件时间原话：{event_raw}；记录于{recorded}。原话：{r['source']['text']}{supporting}（记忆#{item['id']}，第{item['version']}版）")
    return '\n'.join(lines)
