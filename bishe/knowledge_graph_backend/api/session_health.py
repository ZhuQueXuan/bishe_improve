"""Finite, sourced conversation-only user reports; no model or database access.

The parser admits a small explicit grammar. Unsupported statements are not
silently promoted to medical facts. Original statements and event provenance
are retained in memory for this reconstruction only, never in UserMemory.
"""
import copy
import hashlib
import re
from datetime import datetime,timezone
from .memory_contract import validate_record,decide_update

CLAUSES=re.compile(r'[^。！？!?；;，,\n]+')
EXCLUDED=re.compile(r'如果|假如|假设|比如|例如|引用|他说|她说|你说|[“”「」"]')
QUESTION=re.compile(r'什么|哪些|如何|怎么|为什么|是否|有没有|能否|吗|么|想了解|想知道|想问|介绍|查询|讨论|改查|请查|注意什么')
SUBJECT=re.compile(r'我(?:的)?(?:妈妈|母亲|爸爸|父亲|妻子|丈夫|孩子|儿子|女儿)|我(?:本人)?')
RELATIONS={'妈妈':'mother','母亲':'mother','爸爸':'father','父亲':'father','妻子':'wife','丈夫':'husband',
           '孩子':'child','儿子':'son','女儿':'daughter'}
TIME=re.compile(r'上个月|上周|以前|之前|曾经|过去|去年|前年|昨天|今天|现在|目前|最近|\d{4}年(?:\d{1,2}月)?(?:\d{1,2}日)?')
PAST=re.compile(r'上个月|上周|以前|之前|曾经|过去|去年|前年|昨天|得过')
NOW=re.compile(r'现在|目前|今天|最近|又|再次|这次')
REFERENCE=re.compile(r'那次|这次|上次|刚才|前面提到的')
CORRECTION=re.compile(r'更正|纠正|刚才说错|之前说错')
UNCERTAIN=re.compile(r'可能|怀疑|疑似|不确定|好转|(?:没|未|不).{0,4}(?:好|康复|痊愈)')
RESOLVED=re.compile(r'已(?:经)?(?:完全)?(?:好了|康复(?:了)?|痊愈(?:了)?)|好了|康复了|痊愈了|已结束')
DENIED=re.compile(r'没有|没得|没患|不是|并非')
BEFORE=re.compile(r'(?:(?:的|那次|这次|上次|刚才|前面提到的|一直|又|再次|曾经|以前|之前|过去|去年|前年|上周|上个月|昨天|今天|现在|目前|最近)|(?:患有|患上|得了|得过|有|没有|没得|没患|不是|并非|可能有|怀疑有|疑似|不确定有没有)|\d{4}年(?:\d{1,2}月)?(?:\d{1,2}日)?)*')
AFTER=re.compile(r'(?:(?:已经|已|现在|目前|仍|还|完全|那次|这次)|(?:好了|康复了|痊愈了|已结束|结束了|康复|痊愈|没有完全康复|还没好|没完全好|没好|未好|没康复|未康复|好转了|好转|了|过))*')


def actor(text):
    match=SUBJECT.search(text)
    if not match:return None
    for label,key in RELATIONS.items():
        if label in match.group():return 'family:'+key
    return 'self'


class SessionHealth:
    def __init__(self,session,named,owner=None):
        self.session=session;self.owner=owner or 'session:'+session;self.named=named
        self.records=[];self.effective={};self.pending={};self.audit=[];self.sources={}
        self.analyses={};self.focus_subjects=[];self.topic=1
        self.record_topics={};self.links={}
        self.reconstructed_at=datetime.now(timezone.utc).isoformat()
        self.current=self.empty('')

    def empty(self,text):
        return {'handled':False,'query':text,'original':text,'names':None,'subjects':[],'personal':False,
                'clarification':None,'only_update':False,'filtered':False,'report_ids':[],
                'dialogue_text':text,'time_basis':{}}

    def change_topic(self):
        self.topic+=1;self.focus_subjects=[]

    def _candidate(self,name,subject,state,mid,start,end,intent,event_id=None,time_raw=None):
        source=self.sources[mid]
        text=source['content'][start:end]
        raw=time_raw
        rid='report:'+hashlib.sha256(f'{mid}:{start}:{end}:{name}:{subject}'.encode()).hexdigest()[:24]
        record={'id':rid,'owner':self.owner,'session':self.session,'subject':subject,'kind':'event',
            'attribute':'health:'+name,'value':name,'scope':'session','scope_ref':self.session,
            'storage_state':'active','event_state':state,'event_id':event_id,
            'recorded_at':source['recorded_at'],'event_time':{'raw':raw,'start':None,'end':None},
            'source':{'message_id':mid,'start':start,'end':end,'text':text,
                      'mode':'explicit_request' if intent!='new_information' else 'self_report' if subject=='self' else 'family_report'},
            'revision':1}
        return validate_record(record,self.sources)

    def ingest(self,text,mid,recorded_at=None):
        result=self.empty(text);self.current=result
        if mid in self.sources:return self.analyses.get(mid,result)
        basis='stored_message_time' if recorded_at else 'reconstruction_time_original_message_time_unknown'
        self.sources[mid]={'id':mid,'owner':self.owner,'session':self.session,'role':'user',
                           'content':text,'recorded_at':recorded_at or self.reconstructed_at,'time_basis':basis}
        self.analyses[mid]=result
        if not text.strip() or EXCLUDED.search(text):return result
        if '不是我' in text and SUBJECT.search(text):
            result.update(handled=True,clarification='你是在更正说的是谁吗？请分别说明本人和家属的情况。')
            return result
        who=None;lookup=[];questions=[];new_names=[];query_names=[];removed=False
        correction=bool(CORRECTION.search(text))
        for match in CLAUSES.finditer(text):
            raw=match.group();span=raw.strip();start=match.start()+len(raw)-len(raw.lstrip());end=start+len(span)
            if not span:continue
            found_subjects=list(SUBJECT.finditer(span))
            if len(found_subjects)>1:
                result.update(handled=True,clarification='这句话涉及多个人，请分别说明本人和家属的情况。')
                removed=True;continue
            explicit=actor(span)
            if explicit:who=explicit
            names=self.named(span)
            question=bool(QUESTION.search(span) or match.end()<len(text) and text[match.end()] in '？?')
            if question:
                lookup.append(span);questions.append(span);query_names.extend(names)
                if explicit:result['subjects'].append(explicit)
                continue
            if CORRECTION.fullmatch(span) or re.fullmatch(r'(?:刚才|之前)说错了',span):
                removed=True;continue
            if len(names)>1:
                if explicit:
                    result.update(handled=True,clarification='请分别说明每一种情况，避免把不同疾病或事件合在一起。')
                    removed=True
                else:lookup.append(span)
                continue
            if re.search(r'希望|担心|了解|学习|研究|阅读|听说|害怕|想要',span):
                lookup.append(span);continue
            state_uncertain=bool(UNCERTAIN.search(span))
            resolved=bool(RESOLVED.search(span)) and not state_uncertain
            if not names and resolved:
                unnamed=span[found_subjects[0].end():] if found_subjects else span
                if not AFTER.fullmatch(unnamed):lookup.append(span);continue
                possible=[r for key,r in self.effective.items() if (who is None or key[0]==who)
                          and r['event_state'] in {'current','past','uncertain'}]
                if len(possible)!=1:
                    result.update(handled=True,clarification='你说已经好了，是指谁、哪一次提到的情况？')
                    removed=True;continue
                names=[possible[0]['value']];who=possible[0]['subject']
            if not names:lookup.append(span);continue
            name=names[0]
            if name in span and len(list(DENIED.finditer(span[:span.index(name)])))>1:
                result.update(handled=True,clarification='这句话包含多层否定，请直接说明你是在否认、更正，还是仍不确定这项情况。')
                removed=True;continue
            if who is None and resolved:
                possible=[r for (subject,label),r in self.effective.items() if label==name]
                if len(possible)>1:
                    result.update(handled=True,clarification='你说已经好了，是指本人还是哪位家属的情况？')
                    removed=True;continue
                if possible:who=possible[0]['subject']
            if who is None:lookup.append(span);continue
            # Admit only exact predicate/name/suffix spans. Questions, learning,
            # fear, desire and arbitrary text do not become a health assertion.
            body=span[found_subjects[0].end():] if found_subjects else span
            if name in body:
                offset=body.index(name);prefix=body[:offset];suffix=body[offset+len(name):]
                if not BEFORE.fullmatch(prefix) or not AFTER.fullmatch(suffix):lookup.append(span);continue
                if not prefix and not suffix:lookup.append(span);continue
            elif not resolved:lookup.append(span);continue
            state=('uncertain' if state_uncertain else 'resolved' if resolved else 'denied' if DENIED.search(span)
                   else 'past' if PAST.search(span) and not NOW.search(span) else 'current')
            key=(who,name);old=self.effective.get(key)
            # A record's topic is local routing metadata, not an event date.
            same_topic=old is not None and self.record_topics.get(old['id'])==self.topic
            linked=old is not None and same_topic and (bool(REFERENCE.search(span)) or resolved)
            # A different explicitly stated time is not silently linked as a
            # recovery of the previous event, even when its name is identical.
            time_raw=next((m.group() for m in TIME.finditer(span)),None)
            if time_raw and re.match(r'\d{4}年',time_raw) and not NOW.search(span):state='uncertain'
            different_period=bool(time_raw and (PAST.search(time_raw) or re.match(r'\d{4}年',time_raw))
                                  and time_raw!=old['event_time']['raw']) if old else False
            if linked and different_period:
                if REFERENCE.search(span) and old['event_time']['raw']:
                    result.update(handled=True,clarification='你提到的事件时间与前面不同，请明确是同一次还是另一次情况。')
                    removed=True;continue
                linked=False
            if linked and not explicit and len([r for (s,n),r in self.effective.items() if n==name])>1:
                result.update(handled=True,clarification='请明确说的是哪位当事人的那一次情况。')
                removed=True;continue
            intent='correction' if correction else 'same_event_update' if linked and state!=old['event_state'] else 'new_information'
            if old is None:event_id='event:'+hashlib.sha256(f'{mid}:{start}:{who}:{name}'.encode()).hexdigest()[:20]
            elif correction or linked:event_id=old['event_id']
            elif re.search(r'又|再次|另一次',span) or time_raw and time_raw!=old['event_time']['raw']:
                event_id='event:'+hashlib.sha256(f'{mid}:{start}:{who}:{name}'.encode()).hexdigest()[:20]
            else:event_id=None
            # For inherited subjects, keep the binding clause in the source span.
            source_start=0 if not explicit and SUBJECT.search(text[:start]) or correction else start
            if end-source_start>1600:
                result.update(handled=True,clarification='这段描述较长，请分别说明涉及的人和具体情况。')
                removed=True;continue
            row=self._candidate(name,who,state,mid,source_start,end,intent,event_id,time_raw)
            result.update(handled=True);result['subjects'].append(who);result['report_ids'].append(row['id'])
            self.records.append(row);self.record_topics[row['id']]=self.topic
            if old and (linked or correction):
                self.links[row['id']]={'previous_id':old['id'],
                    'basis':'explicit_correction' if correction else 'explicit_episode_reference' if REFERENCE.search(span) else 'unique_named_recovery_in_current_topic'}
            decision=decide_update(old,row,self.sources,intent=intent)
            if old and intent=='new_information' and (old['event_state'],old['value'],old['event_time']['raw'])==(state,name,row['event_time']['raw']):
                # Compatible corroboration does not prove shared episode identity.
                decision={**decision,'action':'no_change','reason':'compatible_observation_no_episode_merge'}
            if key in self.pending and not correction and intent=='new_information' and state in {'current','denied'}:
                decision={**decision,'action':'clarify','reason':'pending_conflict_requires_explicit_resolution'}
            self.audit.append({'old_id':old['id'] if old else None,'new_id':row['id'],**decision})
            if decision['action']=='clarify':self.pending[key]={'old':old,'new':row}
            elif decision['action'] in {'add','event_transition','replace_effective'}:
                self.effective[key]=row;self.pending.pop(key,None)
            elif decision['action']=='keep_separate':
                # Historical additions are retained but do not displace a known
                # current report. A clearly new current episode can become focus.
                if old is None or state=='current':self.effective[key]=row
            if state in {'past','resolved','denied'}:removed=True
            else:lookup.append(span);new_names.append(name)
        result['subjects']=sorted(set(result['subjects']))
        if result['subjects']:self.focus_subjects=result['subjects']
        elif re.search(r'它|这种病|该病|那次|上次|刚才',text):result['subjects']=list(self.focus_subjects)
        result['personal']=bool(result['subjects'])
        if result['handled']:
            # Pending contradictions need an explicit target even for a denial.
            pending_names=[name for (subject,name) in self.pending if subject in result['subjects']
                           and name in self.named(text)]
            result['names']=sorted(set(query_names or new_names or pending_names))
            result['query']='；'.join(lookup) if removed else text
            result['filtered']=result['query']!=text
            result['only_update']=not result['names'] and not questions and not result['clarification']
            result['dialogue_text']='；'.join(questions)
        result['time_basis']={mid:basis}
        return result

    def selected(self,names):
        if not self.current['personal']:return []
        subjects=set(self.current['subjects'])
        period=self.requested_period()
        if period:
            matches=[r for r in self.records if r['value'] in names and r['subject'] in subjects
                     and r['event_time']['raw']==period]
            ids={r['event_id'] for r in matches if r['event_id']}
            # Find the latest sourced observation for each explicitly identified
            # historical episode, including its subsequent reported resolution.
            selected={r['event_id']:r for r in self.records if r['event_id'] in ids}
            return list(selected.values())
        return [r for (s,n),r in self.effective.items() if n in names and s in subjects]

    def requested_period(self):
        text=self.current['original']
        if not QUESTION.search(text):return None
        found=re.search(r'上个月|上周|去年|前年|昨天|\d{4}年(?:\d{1,2}月)?(?:\d{1,2}日)?',text)
        return found.group() if found else None

    def clarification(self,names):
        if self.current['clarification']:return self.current['clarification']
        if self.requested_period() and self.current['personal']:
            selected=self.selected(names)
            if len({r['event_id'] for r in selected})>1:
                return '这个时间范围内提到过不止一次情况，请明确希望回顾哪一次。'
            if selected and all(p['old'] is None or p['old']['event_id']!=selected[0]['event_id'] for p in self.pending.values()):return None
        if self.current['personal'] and any(n in names and s in self.current['subjects'] for s,n in self.pending):
            return '前后说法有差异。你是在更正之前的说法，还是描述另一个时间或另一次事件？'
        return None

    def packet(self,names):
        rows=self.selected(names)
        if not rows:return None
        related=[p['new'] for (s,n),p in self.pending.items() if n in names and s in self.current['subjects']]
        links={r['id']:self.links[r['id']] for r in rows+related if r['id'] in self.links}
        previous_ids={link['previous_id'] for link in links.values()}
        supporting=[r for r in self.records if r['id'] in previous_ids]
        for row in rows+related+supporting:validate_record(row,self.sources)
        return copy.deepcopy({'scope':'current_conversation_only','authority':'user_reports_not_medical_evidence',
            'records':rows,'conflicting_reports':related,
            'event_links':links,'supporting_reports_not_current_state':supporting,
            'source_time_basis':{r['source']['message_id']:self.sources[r['source']['message_id']]['time_basis'] for r in rows+related+supporting},
            'policy':'records为选中的有效用户自述，其余仅供追溯不可恢复为当前状态。区分本人/家属与时间原话。past/resolved/denied不是当前患病；resolved仅为用户报告结束，不是医学确认。不得由旧事件推断新症状因果。'})

    def dialogue_history(self,history):
        rows=[];skip_assistant=False
        for i,message in enumerate(history):
            mid=str(message.get('id',i))
            analysis=self.analyses.get(mid)
            if message.get('role')=='user':
                skip_assistant=bool(analysis and analysis['handled'] and not analysis['dialogue_text'])
                if analysis and analysis['handled']:
                    if analysis['dialogue_text']:rows.append({**message,'content':analysis['dialogue_text']})
                else:rows.append(message)
            elif not skip_assistant:rows.append(message)
        return rows
