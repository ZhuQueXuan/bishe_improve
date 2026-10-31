"""Release-gated normal chat adapter. Background work never runs in this request."""
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.response import Response
from .models import ChatMessage,Conversation
from .natural_memory_models import NaturalMemoryLedger,NaturalMemoryOperation
from .natural_memory_contract import NaturalMemoryContractError,digest,need
from . import natural_memory_background as background,natural_memory_intake as intake,natural_memory_answering as answering

def respond(request):
    user=request.user;body=request.data
    if not background.available() or not getattr(user,'is_authenticated',False):return None
    if not NaturalMemoryLedger.objects.filter(user=user).exists():return None
    permission=answering.grants(user)
    if not body.get('resume_token') and (not permission or not permission['cloud_routing']):return None
    provider=getattr(settings,'NATURAL_MEMORY_CHAT_PROVIDER',None)
    # No provider is installed by default. An explicitly configured bounded
    # adapter must never fall through to an unbudgeted legacy model on failure.
    from .natural_memory_dispatch import provider_for_user
    try:provider=provider_for_user(provider,user)
    except NaturalMemoryContractError:
        return Response({'error':'本次模型任务尚未获准或已结束，消息未发送。'},status=503)
    if provider is None:return None
    resume=body.get('resume_token');pause=body.get('pause_after_steps')
    if resume and (not isinstance(resume,str) or not resume.startswith('nm:')):return None
    try:
        query=body.get('query');need(isinstance(query,str) and 0<len(query.strip())<=4000,'message_budget')
        cid=body.get('conversation_id');key=body.get('client_message_id')
        need(isinstance(key,str) and 0<len(key)<=80,'client_message_id')
        need(pause is None or type(pause) is int and 1<=pause<=3,'pause_steps')
        operation_key='chat-'+digest([cid,key])[:40];fingerprint=digest([cid,key,query])
        if resume:
            from .natural_memory_lifecycle import resume as continue_saved
            # Bind the checkpoint's conversation too; ownership alone is not
            # permission to deliver another session's in-flight answer here.
            from .natural_memory_lifecycle import load_locked
            with intake._locked(user) as locked:
                row=locked;conv=intake._conversation(user,cid)
                checkpoint=load_locked(locked,resume[3:],kind='checkpoint')
                need(checkpoint['status']=='ready' and checkpoint['payload']['source']['session']=='conversation:'+str(cid),'stale_resume')
            result=continue_saved(user,{'checkpoint_key':resume[3:],'expected_revision':row.revision,'expected_epoch':row.epoch},providers=provider)
            message=ChatMessage.objects.get(pk=result['message_id']) if result.get('message_id') else None
        else:
            with intake._locked(user) as row:
                old=NaturalMemoryOperation.objects.filter(user=user,request_key=operation_key).first()
                if old:
                    need(old.input_hash==fingerprint,'request_key_reused')
                    return Response({'status':'already_received','conversation_id':cid,'message_id':old.receipt['message_id'],
                        'error':'这条消息已收到，请刷新历史查看结果。'},status=409)
                conv=intake._conversation(user,cid)
                context_ids=list(conv.messages.filter(id__gt=conv.memory_after_id).order_by('-pk').values_list('pk',flat=True)[:4])[::-1]
                message=ChatMessage.objects.create(conversation=conv,role='user',content=query.strip());message.refresh_from_db()
                NaturalMemoryOperation.objects.create(user=user,request_key=operation_key,input_hash=fingerprint,
                    receipt={'revision':row.revision,'epoch':row.epoch,'message_id':message.pk,'status':'received'})
                background.enqueue_after_chat(user,message.pk,context_ids=context_ids)
                if conv.title=='新对话':conv.title=query[:30];conv.save(update_fields=['title','updated_at'])
            result=answering.run_answer(user,message.pk,context_ids=[],providers=provider,omit_unpermitted_context=True,pause_before_answer=bool(pause))
        if result['status']=='answer_paused':
            answer=ChatMessage.objects.create(conversation=conv,role='assistant',content='资料已准备好，可以继续回答；你也可以直接问新问题。')
            return Response({'query':query,'answer':answer.content,'conversation_id':cid,'conversation_title':conv.title,'has_context':False,
                'user_message':{'id':message.pk,'role':'user','content':message.content,'timestamp':message.created_at.isoformat()},
                'assistant_message':{'id':answer.pk,'role':'assistant','content':answer.content,'timestamp':answer.created_at.isoformat()},
                'retrieval':{'resume_token':'nm:'+result['checkpoint_key']},'memory':{'used_long_term':[]}})
        if result['status'] in ['no_professional_evidence','needs_clarification']:
            wording=('我收到了。你希望我接着具体说明哪方面？' if result['status']=='needs_clarification' else
                '目前没有找到足够的专业资料来回答这个问题。你可以补充具体问题或相关资料，我们继续看。')
            answer=ChatMessage.objects.create(conversation=conv,role='assistant',content=wording)
            return Response({'query':query,'answer':answer.content,'conversation_id':cid,'conversation_title':conv.title,'has_context':False,'sources':[],
                'assistant_message':{'id':answer.pk,'role':'assistant','content':answer.content,'timestamp':answer.created_at.isoformat(),'has_context':False},
                'user_message':{'id':message.pk,'role':'user','content':message.content,'timestamp':message.created_at.isoformat()},
                'memory':{'used_long_term':[],'natural_memory':{'background':'queued_or_disabled','confirmation_required_to_continue_chat':False}}})
        if result['status']!='answered':
            return Response({'error':'本次回答暂未完成，消息已保存。记忆核对不会阻止你继续聊天。',
                'conversation_id':cid,'message_id':message.pk if message else None,'answer_status':result['status']},status=503)
        answer=ChatMessage.objects.get(pk=result['answer_message_id'])
        from .natural_memory_lifecycle import read_delivery
        result=read_delivery(user,result['delivery_key'])
        need(result.get('status')=='answered','stale_answer_context')
        # The response never asks the user to confirm a memory before replying.
        response={'query':query,'answer':result['answer'],'has_context':bool(result['basis']['professional_evidence']),
            'conversation_id':cid,'conversation_title':conv.title,
            'assistant_message':{'id':answer.pk,'role':'assistant','content':answer.content,'timestamp':answer.created_at.isoformat(),
                'has_context':answer.has_context},'sources':[],
            'memory':{'used_long_term':[],'natural_memory':{'background':'queued_or_disabled','confirmation_required_to_continue_chat':False}},
            'natural_basis':result['basis'],'natural_fence':result['fence']}
        if not resume:response['user_message']={'id':message.pk,'role':'user','content':message.content,'timestamp':message.created_at.isoformat()}
        return Response(response)
    except NaturalMemoryContractError as error:return Response({'error':'本次状态已变化，请刷新后重新提问。','reason':str(error)},status=409)
