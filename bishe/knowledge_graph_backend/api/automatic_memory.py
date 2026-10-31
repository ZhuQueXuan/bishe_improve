"""Conservative finite extraction from the current user's own explicit language.

No medical facts, history scans, model extraction, embeddings, or hidden retries.
"""
import hashlib
import re
import uuid
from django.db import transaction
from .models import MemorySettings,UserMemory,MemoryInputEvent
from .user_memory import OPTIONS

from .preference_language import preference_events,is_preference_only

def process_current_input(user,conversation,text,message_id=None):
    if not user.is_authenticated or conversation is None:return {'status':'not_persistent','events':[]}
    events=preference_events(text)
    if not events:return {'status':'no_candidate','events':[]}
    # The official client supplies a stable ID, so retry/replay cannot resurrect a forgotten preference.
    mid=message_id or 'request:'+uuid.uuid4().hex
    event_id=hashlib.sha256(f'{conversation.id}:{mid}'.encode()).hexdigest()
    query_hash=hashlib.sha256(text.encode()).hexdigest()
    with transaction.atomic():
        s,_=MemorySettings.objects.get_or_create(user=user)
        s=MemorySettings.objects.select_for_update().get(pk=s.pk)
        prior=MemoryInputEvent.objects.filter(user=user,event_id=event_id).first()
        if prior:
            return {'status':'already_processed' if prior.input_hash==query_hash else 'message_id_conflict','events':[]}
        applied=[]
        for e in events:
            if e['span'] not in text or len(e['span'])>160:continue
            if e['action']=='save' and not s.auto_enabled:continue
            row=UserMemory.objects.select_for_update().filter(user=user,kind=e['kind']).first()
            if e['action']=='forget':
                if row:
                    from .memory_lifecycle import exclude_preference_source
                    exclude_preference_source(row)
                if row is None:
                    row=UserMemory(user=user,kind=e['kind'],value='',status='forgotten',source='conversation_explicit')
                elif row.status=='forgotten':continue
                else:row.version+=1
                row.value=''; row.status='forgotten'; row.source_text=''; row.source_message_ref=''
            else:
                if e['value'] not in OPTIONS[e['kind']]['values']:continue
                # An incidental self-introduction does not replace a different manual profile.
                if row and row.source=='manual_selection' and row.value!=e['value'] and e['kind']=='background':continue
                if row and row.status=='paused':continue
                if row and row.status=='active' and row.value==e['value']:continue
                if row:row.version+=1
                else:row=UserMemory(user=user,kind=e['kind'])
                row.value=e['value']; row.status='active'; row.source='conversation_explicit'
                row.source_text=e['span']; row.source_message_ref=f'conversation:{conversation.id}/{mid}'[:100]
            row.save(); applied.append({'kind':e['kind'],'action':e['action'],'version':row.version})
        MemoryInputEvent.objects.create(user=user,event_id=event_id,input_hash=query_hash)
        if applied:
            s.version+=1; s.save(update_fields=['version'])
            # Cached derived context is invalid after profile update or forgetting.
            from .models import Conversation
            Conversation.objects.filter(user=user).update(context_notes={})
        return {'status':'applied' if applied else 'unchanged_or_disabled','events':applied}

