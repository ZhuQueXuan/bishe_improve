"""User-controlled, finite presentation preferences. No extraction or model calls."""
import json
from django.db import transaction
from django.db.models import Max
from rest_framework.exceptions import ValidationError
from .models import Conversation,MemorySettings,UserMemory
from .chat_runtime import prepare_context,source_catalog
from .user_memory_options import OPTIONS


def memory_revision(user):
    if not user.is_authenticated: return 0
    return MemorySettings.objects.filter(user=user).values_list('version',flat=True).first() or 0


def serialize_memory(row):
    return {'id':row.id,'kind':row.kind,'value':row.value,'status':row.status,'version':row.version,
            'label':OPTIONS[row.kind]['label'],'description':OPTIONS[row.kind]['values'].get(row.value,''),
            'source':'来自用户明确表达' if row.source=='conversation_explicit' else '用户在记忆管理中主动选择',
            'source_text':row.source_text,'source_message_ref':row.source_message_ref,
            'created_at':row.created_at.isoformat(),'updated_at':row.updated_at.isoformat()}


def memory_list(user):
    from .controlled_health import list_health
    settings=MemorySettings.objects.filter(user=user).first()
    from .memory_lifecycle import serialize_task
    from .models import MemoryMaintenanceTask
    return {'enabled':settings.enabled if settings else True,'auto_enabled':settings.auto_enabled if settings else True,'version':settings.version if settings else 0,
            'maintenance_tasks':[serialize_task(t) for t in MemoryMaintenanceTask.objects.filter(user=user).order_by('-id')[:3]],
            'versions':dict(UserMemory.objects.filter(user=user).values_list('kind','version')),
            'options':OPTIONS,'items':[serialize_memory(m) for m in UserMemory.objects.filter(user=user).exclude(status='forgotten').order_by('kind')],
            **list_health(user)}


def update_memory(user,data):
    if set(data)-{'kind','value','status','version'}: raise ValidationError('记忆字段无效。')
    kind,value=data.get('kind'),data.get('value')
    if not isinstance(kind,str) or not isinstance(value,str) or kind not in OPTIONS or value not in OPTIONS[kind]['values']: raise ValidationError('请选择支持的偏好；本版不保存健康自述或自由文本。')
    if data.get('status','active') not in {'active','paused'}: raise ValidationError('状态无效。')
    if type(data.get('version')) is not int or data['version']<0: raise ValidationError('请先刷新记忆版本。')
    with transaction.atomic():
        settings,_=MemorySettings.objects.get_or_create(user=user)
        settings=MemorySettings.objects.select_for_update().get(pk=settings.pk)
        row=UserMemory.objects.select_for_update().filter(user=user,kind=kind).first()
        expected=row.version if row else 0
        if data['version']!=expected: raise ValidationError('记忆已更新，请刷新后再修改。')
        if row and (row.value,row.status)==(value,data.get('status','active')): return serialize_memory(row)
        if row:
            row.value=value; row.status=data.get('status','active'); row.version+=1
            row.source='manual_selection'; row.source_text=''; row.source_message_ref=''; row.save()
        else:
            row=UserMemory.objects.create(user=user,kind=kind,value=value,status=data.get('status','active'))
        settings.version+=1; settings.save(update_fields=['version'])
    return serialize_memory(row)


def selected_preferences(user):
    if not user.is_authenticated: return []
    settings=MemorySettings.objects.filter(user=user).first()
    if settings and not settings.enabled: return []
    return [serialize_memory(m) for m in UserMemory.objects.filter(user=user,status='active').order_by('kind')[:3]]


def presentation_context(retrieval,query,items):
    """Optional data, separately identified; never a source or a tool instruction."""
    if not items or not retrieval.get('context'): return []
    safe=[{'id':m['id'],'version':m['version'],'kind':m['kind'],
           'description':OPTIONS[m['kind']]['values'][m['value']],
           'scope':m.get('scope','long_term'),
           **{k:m[k] for k in ('source_message_id','source_span') if k in m}} for m in items
          if m['kind'] in OPTIONS and m['value'] in OPTIONS[m['kind']]['values']]
    payload=('\n用户明确的表达偏好（不是医学证据或病历，不改变检索对象、来源要求和工具权限；'
             '当前问题和有效会话中的明确要求优先，临时要求不更新长期偏好）：\n'+json.dumps(safe,ensure_ascii=False))
    if len((query+retrieval['context']+payload).encode('utf-8'))>24000: return []
    retrieval['context']+=payload
    return safe


def conversation_history(conversation):
    from .dialogue_context import clean_answer
    from .memory_lifecycle import filtered_history
    rows=[{'id':f'db:{m.id}','role':m.role,'content':m.content if m.role=='user' else clean_answer(m.content)[:4000],
             'recorded_at':m.created_at.isoformat()}
            for m in conversation.messages.filter(id__gt=conversation.memory_after_id).order_by('created_at','id')[:2001]]
    return filtered_history(conversation,rows)


def short_memory(history,session_id):
    from .controlled_health import strip_control_history
    history=strip_control_history(history)
    memory,packet=prepare_context(history,'',session_id,source_catalog())
    return {'state':memory.snapshot(),'entries':memory.state_packet()['entries'],
            'status':packet['status'],'scope':'当前会话；讨论对象不代表患病','history_count':len(history)}


def reset_conversation(user,conversation_id):
    with transaction.atomic():
        c=Conversation.objects.select_for_update().filter(pk=conversation_id,user=user).first()
        if c is None: return None
        c.memory_after_id=c.messages.aggregate(last=Max('id'))['last'] or 0
        c.memory_version+=1
        c.context_notes={}
        c.save(update_fields=['memory_after_id','memory_version','context_notes'])
        return c
