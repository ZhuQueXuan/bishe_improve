from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from .models import Conversation,MemorySettings,UserMemory
from .user_memory import memory_list,update_memory,conversation_history,short_memory,reset_conversation
from .planning_runtime import BoundaryError


class LongMemoryView(APIView):
    permission_classes=[IsAuthenticated]
    def get(self,request): return Response(memory_list(request.user))
    def post(self,request):
        update_memory(request.user,request.data)
        return Response(memory_list(request.user))
    def patch(self,request):
        fields=set(request.data)-{'version'}
        if not fields or fields-{'enabled','auto_enabled','health_collect_enabled'} or any(type(request.data[k]) is not bool for k in fields) or type(request.data.get('version')) is not int:
            raise ValidationError('开关或版本无效。')
        with transaction.atomic():
            s,_=MemorySettings.objects.get_or_create(user=request.user)
            s=MemorySettings.objects.select_for_update().get(pk=s.pk)
            if s.version!=request.data['version']: raise ValidationError('设置已更新，请刷新。')
            if any(getattr(s,k)!=request.data[k] for k in fields):
                for k in fields:setattr(s,k,request.data[k])
                s.version+=1; s.save()
        return Response(memory_list(request.user))


class ForgetMemoryView(APIView):
    permission_classes=[IsAuthenticated]
    def post(self,request,memory_id):
        with transaction.atomic():
            s,_=MemorySettings.objects.get_or_create(user=request.user)
            s=MemorySettings.objects.select_for_update().get(pk=s.pk)
            row=UserMemory.objects.select_for_update().filter(pk=memory_id,user=request.user).first()
            if row is None: return Response({'error':'记忆不存在。'},status=404)
            if type(request.data.get('version')) is not int or request.data['version']!=row.version:
                raise ValidationError('记忆已更新，请刷新。')
            if row.status!='forgotten':
                from .memory_lifecycle import exclude_preference_source
                exclude_preference_source(row)
                row.value=''; row.status='forgotten'; row.source_text=''; row.source_message_ref=''; row.version+=1; row.save()
                s.version+=1; s.save(update_fields=['version'])
                Conversation.objects.filter(user=request.user).update(context_notes={})
        return Response(memory_list(request.user))


class ConversationMemoryView(APIView):
    permission_classes=[IsAuthenticated]
    def get(self,request,conversation_id):
        c=Conversation.objects.filter(pk=conversation_id,user=request.user).first()
        if c is None: return Response({'error':'会话不存在。'},status=404)
        try: result=short_memory(conversation_history(c),f'conversation:{c.id}')
        except BoundaryError: return Response({'error':'当前记忆过长，请重置后继续。'},status=400)
        return Response({**result,'version':c.memory_version})
    def post(self,request,conversation_id):
        if request.data.get('action')!='reset': raise ValidationError('操作无效。')
        c=reset_conversation(request.user,conversation_id)
        if c is None: return Response({'error':'会话不存在。'},status=404)
        return Response({**short_memory([],f'conversation:{c.id}'),'version':c.memory_version})


class GuestMemoryView(APIView):
    def post(self,request):
        history=request.data.get('history',[])
        if not isinstance(history,list) or len(history)>2000: raise ValidationError('会话过长。')
        rows=[]
        for i,m in enumerate(history):
            if not isinstance(m,dict) or m.get('role') not in {'user','assistant'} or not isinstance(m.get('content'),str) or len(m['content'])>4000:
                raise ValidationError('历史格式无效。')
            rows.append({'id':str(i),'role':m['role'],'content':m['content'] if m['role']=='user' else ''})
        try: return Response(short_memory(rows,'guest:preview'))
        except BoundaryError: raise ValidationError('当前记忆超出范围，请重置。')


class HealthProposalConfirmView(APIView):
    permission_classes=[IsAuthenticated]
    def post(self,request,proposal_id):
        from .controlled_health import confirm_proposal
        from django.db import DatabaseError
        try:return Response(confirm_proposal(request.user,proposal_id,request.data))
        except DatabaseError:return Response({'error':'持久保存未完成，请刷新后核对；没有确认保存成功。'},status=503)


class HealthMemoryChangeView(APIView):
    permission_classes=[IsAuthenticated]
    def post(self,request,memory_id):
        from .controlled_health import change_health
        from django.db import DatabaseError
        try:return Response(change_health(request.user,memory_id,request.data))
        except DatabaseError:return Response({'error':'记忆操作未完成，请刷新后核对；没有确认忘记或状态变更成功。'},status=503)


class MemoryMaintenanceView(APIView):
    permission_classes=[IsAuthenticated]
    def post(self,request):
        from .memory_lifecycle import create_task,run_task,serialize_task
        from django.db import DatabaseError
        if set(request.data)!={'request_key','version'}:raise ValidationError('整理字段无效。')
        try:
            task=create_task(request.user,request.data['request_key'],request.data['version'])
            task=run_task(request.user,task.id)
            return Response(serialize_task(task),status=503 if task.status in {'failed','exhausted'} else 200)
        except DatabaseError:return Response({'error':'整理尚未完成，请刷新查看任务状态后重试。'},status=503)
