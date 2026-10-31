from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.contrib.auth.models import User
from django.contrib.auth import authenticate
from django.conf import settings
from django.db import transaction
from django.http import FileResponse, Http404
from collections.abc import Mapping
import re
from rest_framework.authtoken.models import Token
from .utils import DeepSeekAPI
from .models import Conversation, ChatMessage
from .graph_retrieval import retrieve_context, RetrievalUnavailable, graph_statistics
from .evidence_sources import source_appendix, public_sources, stored_sources
from .document_search import DocumentSearch, production_library, SCOPES
from .agent_loop import AgentFailure
from .fulltext_retrieval import retrieve_phrase
from .chat_runtime import ChatAgent, BoundaryError
from .user_memory import conversation_history as effective_history, memory_revision,selected_preferences,presentation_context
from .automatic_memory import process_current_input,is_preference_only
from .preference_language import effective_presentation
from .dialogue_context import clean_answer
from .controlled_health import command_response,strip_control_history,context_health,attach_health
from django.db import DatabaseError, IntegrityError
from rest_framework.exceptions import ValidationError
from .account_credentials import login_username, registration_fields, registration_conflict

def serialize_message(message):
    return {
        'id': message.id,
        'role': message.role,
        'content': message.content,
        'has_context': message.has_context,
        'timestamp': message.created_at.isoformat(),
        'sources': stored_sources(message.content) if message.role == 'assistant' else [],
    }


def serialize_conversation(conversation, include_messages=True):
    data = {
        'id': conversation.id,
        'title': conversation.title,
        'created_at': conversation.created_at.isoformat(),
        'updated_at': conversation.updated_at.isoformat(),
        'memory_version': conversation.memory_version,
    }
    if include_messages:
        data['messages'] = [serialize_message(item) for item in conversation.messages.all()]
    return data


class ConversationListCreateView(APIView):
    """登录用户的会话列表和新建会话。"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        conversations = (
            Conversation.objects.filter(user=request.user)
            .prefetch_related('messages')
            .order_by('-updated_at')[:100]
        )
        return Response([serialize_conversation(item) for item in conversations])

    def post(self, request):
        title = str(request.data.get('title') or '新对话').strip()[:120] or '新对话'
        conversation = Conversation.objects.create(user=request.user, title=title)
        return Response(serialize_conversation(conversation), status=status.HTTP_201_CREATED)


class ConversationDetailView(APIView):
    """只允许用户读取、改名或删除自己的会话。"""
    permission_classes = [IsAuthenticated]

    @staticmethod
    def get_conversation(request, conversation_id):
        return (
            Conversation.objects.filter(id=conversation_id, user=request.user)
            .prefetch_related('messages')
            .first()
        )

    def get(self, request, conversation_id):
        conversation = self.get_conversation(request, conversation_id)
        if not conversation:
            return Response({'error': '会话不存在'}, status=status.HTTP_404_NOT_FOUND)
        return Response(serialize_conversation(conversation))

    def patch(self, request, conversation_id):
        conversation = self.get_conversation(request, conversation_id)
        if not conversation:
            return Response({'error': '会话不存在'}, status=status.HTTP_404_NOT_FOUND)
        title = str(request.data.get('title') or '').strip()
        if not title:
            return Response({'error': '会话标题不能为空'}, status=status.HTTP_400_BAD_REQUEST)
        conversation.title = title[:120]
        conversation.save()
        return Response(serialize_conversation(conversation))

    def delete(self, request, conversation_id):
        conversation = self.get_conversation(request, conversation_id)
        if not conversation:
            return Response({'error': '会话不存在'}, status=status.HTTP_404_NOT_FOUND)
        conversation.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class RegisterView(APIView):
    """用户注册视图"""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            username, password, email = registration_fields(request.data)
        except ValueError as error:
            return Response({'error': str(error)}, status=status.HTTP_400_BAD_REQUEST)
        try:
            with transaction.atomic():
                conflict = registration_conflict(username, email)
                if conflict:
                    return Response({'error': conflict}, status=status.HTTP_400_BAD_REQUEST)
                User.objects.create_user(username=username, password=password, email=email)
        except IntegrityError:
            return Response({'error': '注册信息已被使用，请登录或更换用户名'}, status=status.HTTP_400_BAD_REQUEST)
        except DatabaseError:
            return Response({'error': '账号服务暂时不可用，请稍后重试'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'message': '注册成功'}, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    """用户登录视图"""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        credentials = request.data if isinstance(request.data, Mapping) else {}
        username = credentials.get('username')
        password = credentials.get('password')
        
        if (not isinstance(username, str) or not username.strip() or len(username) > 254
                or not isinstance(password, str) or not password or len(password) > 1024):
            return Response({
                'error': '请提供用户名或邮箱和密码'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        user = authenticate(request=request, username=login_username(username), password=password)
        
        if user:
            token, _ = Token.objects.get_or_create(user=user)
            return Response({
                'token': token.key,
                'user': {
                    'id': user.id,
                    'username': user.username,
                    'email': user.email
                }
            })
        else:
            return Response({
                'error': '用户名或邮箱或密码错误；若邮箱用于多个旧账号，请使用注册用户名登录'
            }, status=status.HTTP_401_UNAUTHORIZED)


class StatisticsView(APIView):
    """Read-only statistics for the current completed full graph."""
    def get(self, request):
        try:
            return Response(graph_statistics())
        except RetrievalUnavailable as error:
            return Response({'error': str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class KnowledgeQAView(APIView):
    """
    基于知识图谱的智能问答API
    """
    def post(self, request, format=None):
        from .natural_memory_chat import respond as natural_chat_response
        integrated = natural_chat_response(request)
        if integrated is not None:
            return integrated
        query = request.data.get('query')
        conversation_id = request.data.get('conversation_id')
        request_history = request.data.get('history', [])
        
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            return Response(
                {"error": "请输入不超过4000字的问题"},
                status=status.HTTP_400_BAD_REQUEST
            )

        conversation = None
        if conversation_id is not None:
            if not request.user.is_authenticated:
                return Response(
                    {'error': '登录后才能保存会话'},
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            conversation = Conversation.objects.filter(
                id=conversation_id,
                user=request.user,
            ).first()
            if not conversation:
                return Response({'error': '会话不存在'}, status=status.HTTP_404_NOT_FOUND)

        conversation_history = []
        if conversation:
            conversation_history = effective_history(conversation)
        elif isinstance(request_history, list):
            if len(request_history) > 2000:
                return Response({'error': '本次会话过长，请新建对话继续。'}, status=status.HTTP_400_BAD_REQUEST)
            for item in request_history:
                if not isinstance(item, dict):
                    continue
                role = item.get('role')
                content = str(item.get('content', '')).strip()
                if role in {'user', 'assistant'} and content:
                    if role == 'user' and len(content) > 4000:
                        return Response({'error': '历史问题超过4000字，请新建对话。'}, status=status.HTTP_400_BAD_REQUEST)
                    mid = str(item.get('id', len(conversation_history)))
                    if len(mid) > 80:
                        return Response({'error': '历史消息标识无效。'}, status=status.HTTP_400_BAD_REQUEST)
                    conversation_history.append({'id': 'history:' + mid, 'role': role, 'content': content if role == 'user' else clean_answer(content)[:4000]})
        
        pause = request.data.get('pause_after_steps')
        resume = request.data.get('resume_token')
        guest_session = request.data.get('session_id', 'anonymous')
        if ((pause is not None and (type(pause) is not int or not 1 <= pause <= 3))
                or (resume is not None and (not isinstance(resume, str) or len(resume) > 2048))
                or not isinstance(guest_session, str) or not 0 < len(guest_session) <= 100):
            return Response({'error': '任务参数无效。'}, status=status.HTTP_400_BAD_REQUEST)
        session_id = f'conversation:{conversation.id}' if conversation else f'guest:{guest_session}'
        client_mid=request.data.get('client_message_id')
        if client_mid is not None and (not isinstance(client_mid,str) or not 0<len(client_mid)<=80):
            return Response({'error':'消息标识无效。'},status=400)
        from .natural_memory_background import available as natural_memory_available
        from .natural_memory_models import NaturalMemoryLedger
        natural_mode = (natural_memory_available() and request.user.is_authenticated and
                        NaturalMemoryLedger.objects.filter(user=request.user).exists())
        try:
            control=None if natural_mode else command_response(request.user,conversation,query,client_mid)
        except ValidationError as error:
            return Response({'error':' '.join(map(str,error.detail))},status=400)
        except DatabaseError:
            return Response({'error':'保存请求未完成，未确认长期保存。请在记忆设置核对后重试。'},status=503)
        if control is not None:return Response(control)
        loaded_history_cursor=max([conversation.memory_after_id if conversation else 0]+[
            int(row['id'][3:]) for row in conversation_history
            if isinstance(row.get('id'),str) and row['id'].startswith('db:') and row['id'][3:].isdigit()])
        loaded_history_cursor=getattr(conversation_history,'source_cursor',loaded_history_cursor)
        conversation_history=strip_control_history(conversation_history)
        from .memory_retrieval import is_recall_query
        auto_memory=({'status':'background_collection','events':[]} if natural_mode else
                     {'status':'recall_read_only','events':[]} if is_recall_query(query)
                     else process_current_input(request.user,conversation,query,client_mid))
        if auto_memory['status']=='message_id_conflict':
            return Response({'error':'消息标识已使用，请重新提交。'},status=409)
        memory_version = conversation.memory_version if conversation else 0
        profile_version = memory_revision(request.user)
        if memory_version or profile_version:
            session_id += f':m{memory_version}:p{profile_version}'
        owner = {'user': request.user.id if request.user.is_authenticated else None, 'session': session_id}
        history_cursor=loaded_history_cursor

        def memory_changed():
            return (memory_revision(request.user)!=profile_version or
                    (conversation is not None and not Conversation.objects.filter(
                        pk=conversation.pk,user=request.user,memory_version=memory_version).exists()) or
                    (conversation is not None and conversation.messages.filter(id__gt=history_cursor).exists()))

        def changed_response():
            return Response({'error':'记忆或会话已更新，本次旧状态结果已停止，请重新提问。'},status=409)
        try:
            agent = ChatAgent(retrieve_context, retrieve_phrase, DeepSeekAPI, history=conversation_history,
                query=query, session_id=session_id, owner=owner, resume_token=resume, pause_after_steps=pause,
                dialogue_cache=conversation.context_notes if conversation else None,
                context_revision=f'{memory_version}:{profile_version}',
                document_search=DocumentSearch() if settings.DOCUMENT_RAG_ENABLED else None)
            agent.memory.health.discussed_names=agent.memory.snapshot()['objects']
            continuation=agent.continuation()
            selection_query=continuation['continuation']['selection_query'] if continuation else query
            health_rows,health_conflict=([],None) if natural_mode else context_health(request.user,selection_query,agent.memory.health)
            personal_selection=getattr(agent.memory.health,'retrieval_plan',{})
            retrieval_query=personal_selection.get('resolved_query',query) if not resume else query
            if retrieval_query!=query:agent.answer_query=retrieval_query
            preferences=[] if natural_mode else selected_preferences(request.user)
            choices=(continuation['continuation']['binding']['presentation'] if continuation else
                     effective_presentation(preferences,conversation_history,query,
                         guest=not bool(conversation),requirements=agent.memory.requirements))
            agent.bind_personal(health_rows,preferences,choices,profile_version,memory_version,agent.user_chain)
            import json
            bound_packet=continuation['context'] if continuation else agent.packet
            if health_rows and len(((bound_packet.get('payload') or '')+json.dumps(health_rows,ensure_ascii=False)).encode('utf-8'))>12000:
                return Response({'error':'相关个人状态超过上下文预算，请指定具体事件时间或整理长期记忆。','has_context':False},status=400)
            retrieval = (agent._attach({**agent._finish([], 'long_term_health_conflict',status='ambiguous'),'clarification':health_conflict})
                         if health_conflict and not resume else
                         agent._attach(agent._finish([], 'personal_memory_recall',status='memory_recall'))
                         if personal_selection.get('local_answer') is not None and not resume else
                         agent._attach(agent._finish([], 'preference_only',status='preference_ack'))
                         if is_preference_only(query) and not resume else agent.retrieve(retrieval_query, conversation_history))
        except BoundaryError:
            return Response({'error': '会话状态或任务进度无效、过期或已使用，请新建查询。', 'has_context': False},
                            status=status.HTTP_400_BAD_REQUEST)
        except AgentFailure as error:
            return Response({'error': str(error), 'has_context': False,
                             'retrieval': {'agent': error.trace}}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        context = retrieval['context']
        if memory_changed(): return changed_response()
        used_memories = []
        if agent.trace['stop_reason'] == 'deadline_exceeded':
            return Response({'error': '本次查询等待超时，请稍后重试。', 'has_context': False,
                             'retrieval': {k: v for k, v in retrieval.items() if k != 'context'}},
                            status=status.HTTP_504_GATEWAY_TIMEOUT)
        if retrieval.get('resume_token'):
            selected = '、'.join(retrieval['diseases'])
            answer = f'已查到关联资料，本次选定对象：{selected}（按名称排序，最多2个）。进度已保存，请在剩余时间内点击“继续整理”。'
        elif not context:
            answers = {'ambiguous': '问题涉及多个疾病，请明确希望查询哪一种。',
                       'health_ack':'已按你本次自述更新当前会话中的情况；这不代表医学确认。',
                       'preference_ack':'好的，会参考你的表达要求。',
                       'nonmedical': '当前仅支持医疗知识资料查询，请输入相关问题。',
                       'unsupported_field': '当前图谱未收录经过核验的药品商品名、厂家或品牌资料，无法据此回答。'}
            answer = ('请明确希望展开上一次回答的哪一点，或补充具体问题。' if agent.trace['stop_reason']=='dialogue_reference_unclear' else
                      answers.get(retrieval['status'], '现有资料不足以完整回答这个问题。请补充具体疾病名称或缩小问题范围；不能据此提供诊断或用药建议。'))
            if retrieval.get('clarification'):answer=retrieval['clarification']
            if retrieval['status']=='memory_recall':
                answer=personal_selection['local_answer']
                used_memories=personal_selection.get('preference_items',[])
            if retrieval['status']=='preference_ack':
                action=agent.memory.requirements.last_action
                if action=='undone':answer='已撤销刚才的会话要求，将采用撤销后仍有效的设置。'
                elif action=='nothing_to_undo':answer='当前没有可撤销的会话要求，请说明希望调整哪项设置。'
                elif action=='long_term_needs_explicit_change':answer='刚才涉及长期偏好，请明确希望改成什么，或说明要忘记哪项偏好。'
        else:
            attach_health(retrieval,health_rows)
            used_memories = presentation_context(retrieval,agent.answer_query,choices)
            answer = agent.answer(query, retrieval, conversation_history)
            if answer is None:
                timeout = agent.trace['stop_reason'] == 'deadline_exceeded'
                if agent.trace['stop_reason']=='context_limit':
                    return Response({'error':'本次资料超过上下文预算，请缩小问题范围。','has_context':False},status=400)
                return Response({'error': '本次回答等待超时，请稍后重试。' if timeout else '回答服务暂时不可用，请稍后重试。',
                                 'has_context': True,
                                 'retrieval': {k: v for k, v in retrieval.items() if k != 'context'}},
                                status=status.HTTP_504_GATEWAY_TIMEOUT if timeout else status.HTTP_503_SERVICE_UNAVAILABLE)
            answer += source_appendix(retrieval['facts'])
        if retrieval.get('notice'):
            answer = retrieval['notice'] + '\n\n' + answer

        if memory_changed(): return changed_response()

        response_data = {
            "query": query,
            "answer": answer,
            "has_context": bool(context),
            "sources": public_sources(retrieval['facts']) if context else []
        }
        response_data['memory'] = {'short':retrieval.get('session',{}),
                                  'used_long_term':used_memories,'profile_version':profile_version,
                                  'conversation_version':memory_version}
        response_data['memory']['automatic_processing']=auto_memory
        response_data['memory']['used_health']=[{'id':r['id'],'version':r['version']} for r in health_rows] if context or retrieval['status']=='memory_recall' else []
        response_data['memory']['selection']=personal_selection.get('audit',{})
        response_data['memory']['dialogue']={k:agent.dialogue[k] for k in ['version','coverage_ids','summary','oversized_recent_omitted','reused_note_count','omissions','clarification','layers']}

        if retrieval is not None:
            response_data['retrieval'] = {key: value for key, value in retrieval.items() if key != 'context'}

        if conversation:
            with transaction.atomic():
                conversation = Conversation.objects.select_for_update().get(pk=conversation.pk,user=request.user)
                if memory_changed(): return changed_response()
                user_message = ChatMessage.objects.create(conversation=conversation,role='user',content=query)
                from .natural_memory_background import enqueue_after_chat
                queue_context = [int(m['id'][3:]) for m in conversation_history
                    if isinstance(m.get('id'),str) and m['id'].startswith('db:')][-4:]
                enqueue_after_chat(request.user,user_message.pk,context_ids=queue_context)
                from .memory_lifecycle import link_input_source
                link_input_source(request.user,conversation,client_mid,user_message)
                assistant_message = ChatMessage.objects.create(conversation=conversation,role='assistant',content=answer,has_context=bool(context))
                if conversation.title == '新对话':
                    conversation.title = f'{query[:30]}…' if len(query) > 30 else query
                conversation.context_notes=agent.dialogue
                conversation.save(update_fields=['title','updated_at','context_notes'])
            response_data.update({
                'conversation_id': conversation.id,
                'conversation_title': conversation.title,
                'user_message': serialize_message(user_message),
                'assistant_message': serialize_message(assistant_message),
            })

        return Response(response_data)


class DocumentFileView(APIView):
    """Serve only reviewed, hash-verified public originals; never a user-supplied path."""
    authentication_classes = []
    permission_classes = []

    def get(self, request, version_id):
        import json
        if not re.fullmatch('[0-9a-f]{64}', version_id):
            raise Http404
        try:
            admitted = json.loads(SCOPES.read_text(encoding='utf-8'))['documents']
            if version_id not in {row['version_id'] for row in admitted}:
                raise Http404
            library = production_library()
            row = next(r for r in library.versions() if r['id'] == version_id)
            library.verify(version_id)
            path = library.root / 'objects' / (row['file_hash'] + '.pdf')
            response = FileResponse(path.open('rb'), content_type='application/pdf',
                                    filename=row['metadata']['title'] + '.pdf')
            response['X-Content-Type-Options'] = 'nosniff'
            response['Cache-Control'] = 'private, max-age=300'
            return response
        except Http404:
            raise
        except Exception:
            raise Http404 from None
