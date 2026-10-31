"""Stage A: validate sourced structured records and propose non-mutating decisions.

Stage B uses this pure contract after finite language extraction. Source-span
matching alone is not semantic extraction: the parser must prove the supplied
subject, value, mode, intent and event identity. Persistence adapters must check
permissions and revisions again before any future long-term write.
"""
import copy
from datetime import datetime

VERSION='memory-record-v1'
FIELDS={'id','owner','session','subject','kind','attribute','value','scope','scope_ref',
        'storage_state','event_state','event_id','recorded_at','event_time','source','revision'}
KINDS={'preference','background','event','constraint'}
SCOPES={'current','topic','session','long_term'}
STORAGE={'active','paused','superseded','forgotten'}
EVENT_STATES={'current','past','resolved','denied','uncertain'}
MODES={'explicit_request','self_report','family_report','question','hypothetical','quotation','assistant_inference'}
INELIGIBLE={'question','hypothetical','quotation','assistant_inference'}
INTENTS={'new_information','new_default','correction','same_event_update','forget','pause'}
SCOPE_RANK={'current':0,'topic':1,'session':2,'long_term':3}


class MemoryContractError(ValueError):
    pass


def require(condition,code):
    if not condition:raise MemoryContractError(code)


def bounded(value,limit=160,empty=False):
    return isinstance(value,str) and (empty or bool(value.strip())) and len(value)<=limit


def timestamp(value):
    require(bounded(value,80),'timestamp')
    try:parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    except (ValueError,TypeError):raise MemoryContractError('timestamp') from None
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None,'timestamp_timezone')
    return parsed


def validate_record(record,messages):
    """Return a detached copy; Unicode offsets refer to the original user source."""
    require(isinstance(record,dict) and set(record)==FIELDS,'record_fields')
    for key in ('id','owner','session','attribute'):require(bounded(record[key]),key)
    require(bounded(record['value'],1200,empty=record['storage_state']=='forgotten'),'value')
    for key,choices in [('kind',KINDS),('scope',SCOPES),('storage_state',STORAGE)]:
        require(isinstance(record[key],str) and record[key] in choices,key)
    require(type(record['revision']) is int and record['revision']>=1,'revision')
    require(bounded(record['subject'],80) and (record['subject'] in {'self','unknown'}
            or record['subject'].startswith('family:') and len(record['subject'])>7),'subject')
    require(record['scope_ref'] is None if record['scope']=='long_term' else bounded(record['scope_ref']),'scope_ref')
    if record['scope']=='session':require(record['scope_ref']==record['session'],'scope_ref_session')
    require(record['event_id'] is None or bounded(record['event_id']),'event_id')
    if record['kind']=='event':
        require(isinstance(record['event_state'],str) and record['event_state'] in EVENT_STATES,'event_state')
    else:
        require(record['event_state']=='not_applicable' and record['event_id'] is None,'non_event_state')
    recorded=timestamp(record['recorded_at'])
    source=record['source']
    require(isinstance(source,dict) and set(source)=={'message_id','start','end','text','mode'},'source_fields')
    require(bounded(source['message_id']) and bounded(source['text'],1600),'source_identity')
    require(isinstance(source['mode'],str) and source['mode'] in MODES,'source_mode')
    require(type(source['start']) is int and type(source['end']) is int,'source_offsets')
    require(isinstance(messages,dict),'source_messages')
    message=messages.get(source['message_id'])
    require(isinstance(message,dict),'source_missing')
    require(message.get('id')==source['message_id'] and message.get('role')=='user','source_user')
    require(message.get('owner')==record['owner'] and message.get('session')==record['session'],'source_partition')
    require(isinstance(message.get('content'),str) and 0<=source['start']<source['end']<=len(message['content']),'source_offsets')
    require(message['content'][source['start']:source['end']]==source['text'],'source_span')
    require(recorded==timestamp(message.get('recorded_at')),'recorded_at_source')
    if source['mode']=='self_report':require(record['subject']=='self','self_report_subject')
    if source['mode']=='family_report':require(record['subject'].startswith('family:'),'family_report_subject')
    event_time=record['event_time']
    require(isinstance(event_time,dict) and set(event_time)=={'raw','start','end'},'event_time_fields')
    raw=event_time['raw']
    require(raw is None or bounded(raw,120) and raw in source['text'],'event_time_source')
    start=timestamp(event_time['start']) if event_time['start'] is not None else None
    end=timestamp(event_time['end']) if event_time['end'] is not None else None
    require((start is None and end is None) or raw is not None,'event_time_normalization_source')
    require(start is None or end is None or start<=end,'event_time_interval')
    return copy.deepcopy(record)


def decide_update(old,new,messages,*,intent='new_information'):
    """Propose an action only; never mutate records, histories, caches or storage."""
    require(isinstance(intent,str) and intent in INTENTS,'intent')
    new=validate_record(new,messages)
    old=validate_record(old,messages) if old is not None else None
    def result(action,reason):
        return {'contract':VERSION,'action':action,'reason':reason,'new_id':new['id'],
                'old_id':old['id'] if old else None,'expected_old_revision':old['revision'] if old else None,
                'writes_performed':False,'clinical_confirmation':False}
    if new['source']['mode'] in INELIGIBLE:return result('reject','not_an_asserted_user_memory')
    if new['storage_state']!='active':return result('reject','proposal_not_active')
    if intent!='new_information' and new['source']['mode']!='explicit_request':
        return result('clarify','explicit_update_evidence_required')
    if old is None:
        return result('clarify','update_target_missing') if intent in {'correction','same_event_update','forget','pause'} else result('add','new_sourced_record')
    if old['owner']!=new['owner']:return result('reject','different_owner')
    if new['id']==old['id']:
        return result('no_change','identical_record_replay') if new==old and intent=='new_information' else result('reject','immutable_record_id')
    same_source=(old['source']['message_id']==new['source']['message_id']
                 and old['source']['start']==new['source']['start'] and old['source']['end']==new['source']['end'])
    if old['storage_state']=='forgotten' and same_source:return result('reject','forgotten_source_replay')
    if old['subject']=='unknown' or new['subject']=='unknown':return result('clarify','subject_not_resolved')
    if old['subject']!=new['subject']:
        return result('clarify','update_subject_mismatch') if intent!='new_information' else result('keep_separate','different_subjects')
    if (old['kind'],old['attribute'])!=(new['kind'],new['attribute']):
        return result('clarify','update_attribute_mismatch') if intent!='new_information' else result('keep_separate','different_attributes')
    if old['session']!=new['session'] and old['scope']!='long_term':
        return result('clarify','update_conversation_mismatch') if intent!='new_information' else result('keep_separate','different_conversation_scopes')
    if old['storage_state']!='active':
        return result('clarify','inactive_record_requires_explicit_policy')
    if intent in {'forget','pause'}:
        return result(intent,'explicit_lifecycle_request')
    if timestamp(new['recorded_at'])<timestamp(old['recorded_at']):
        return result('clarify','older_source_cannot_silently_override_newer_record')
    if SCOPE_RANK[new['scope']]<SCOPE_RANK[old['scope']]:
        return result('temporary_override','narrower_scope_does_not_update_default')
    if (new['scope'] in {'topic','current'} and old['scope']==new['scope']
            and new['scope_ref']!=old['scope_ref']):
        return result('keep_separate','different_scope_instances')
    if old['kind']=='event':
        if intent=='same_event_update':
            if old['event_id'] is None or old['event_id']!=new['event_id']:
                return result('clarify','same_event_not_established')
            return result('event_transition','explicit_same_event_transition_user_report_only')
        if old['event_id'] and new['event_id'] and old['event_id']!=new['event_id']:
            return result('keep_separate','different_events')
        if old['event_state']!=new['event_state'] and 'past' in {old['event_state'],new['event_state']}:
            return result('keep_separate','historical_and_current_reports')
        if intent=='correction':
            if old['event_id'] is None or old['event_id']!=new['event_id']:
                return result('clarify','correction_event_target_not_established')
            return result('replace_effective','explicit_event_correction_keep_history')
        return result('clarify','event_link_or_transition_requires_evidence')
    if intent=='same_event_update':return result('reject','not_an_event')
    if intent=='new_default':
        return result('replace_effective','explicit_new_long_term_default') if new['scope']=='long_term' else result('clarify','new_default_scope_not_long_term')
    if intent=='correction':return result('replace_effective','explicit_correction_keep_history')
    if (old['value'],old['scope'],old['scope_ref'])==(new['value'],new['scope'],new['scope_ref']):
        return result('no_change','same_effective_value')
    return result('clarify','newer_record_time_is_not_update_evidence')
