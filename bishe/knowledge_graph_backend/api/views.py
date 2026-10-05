from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.contrib.auth.models import User
from django.contrib.auth import authenticate
from rest_framework.authtoken.models import Token
from .utils import DeepSeekAPI
from .models import Conversation, ChatMessage
from .graph_retrieval import retrieve_context, RetrievalUnavailable, source_appendix, graph_statistics
from .agent_loop import AgentFailure
from .fulltext_retrieval import retrieve_phrase
from .chat_runtime import ChatAgent, BoundaryError

def serialize_message(message):
    return {
        'id': message.id,
        'role': message.role,
        'content': message.content,
        'has_context': message.has_context,
        'timestamp': message.created_at.isoformat(),
    }


def serialize_conversation(conversation, include_messages=True):
    data = {
        'id': conversation.id,
        'title': conversation.title,
        'created_at': conversation.created_at.isoformat(),
        'updated_at': conversation.updated_at.isoformat(),
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
    def post(self, request):
        username = request.data.get('username')
        password = request.data.get('password')
        email = request.data.get('email')
        
        if not all([username, password, email]):
            return Response({
                'error': '请提供所有必需的字段'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            user = User.objects.create_user(
                username=username,
                password=password,
                email=email
            )
            return Response({
                'message': '注册成功'
            }, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({
                'error': '注册失败，用户名可能已存在'
            }, status=status.HTTP_400_BAD_REQUEST)


class LoginView(APIView):
    """用户登录视图"""
    def post(self, request):
        username = request.data.get('username')
        password = request.data.get('password')
        
        if not all([username, password]):
            return Response({
                'error': '请提供用户名和密码'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        user = authenticate(username=username, password=password)
        
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
                'error': '用户名或密码错误'
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
            recent_messages = list(conversation.messages.order_by('created_at', 'id')[:2001])
            conversation_history = [
                {'id': f'db:{item.id}', 'role': item.role, 'content': item.content if item.role == 'user' else ''}
                for item in recent_messages
            ]
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
                    conversation_history.append({'id': 'history:' + mid, 'role': role, 'content': content if role == 'user' else ''})
        
        pause = request.data.get('pause_after_steps')
        resume = request.data.get('resume_token')
        guest_session = request.data.get('session_id', 'anonymous')
        if ((pause is not None and (type(pause) is not int or not 1 <= pause <= 3))
                or (resume is not None and (not isinstance(resume, str) or len(resume) > 2048))
                or not isinstance(guest_session, str) or not 0 < len(guest_session) <= 100):
            return Response({'error': '任务参数无效。'}, status=status.HTTP_400_BAD_REQUEST)
        session_id = f'conversation:{conversation.id}' if conversation else f'guest:{guest_session}'
        owner = {'user': request.user.id if request.user.is_authenticated else None, 'session': session_id}
        try:
            agent = ChatAgent(retrieve_context, retrieve_phrase, DeepSeekAPI, history=conversation_history,
                query=query, session_id=session_id, owner=owner, resume_token=resume, pause_after_steps=pause)
            retrieval = agent.retrieve(query, conversation_history)
        except BoundaryError:
            return Response({'error': '会话状态或任务进度无效、过期或已使用，请新建查询。', 'has_context': False},
                            status=status.HTTP_400_BAD_REQUEST)
        except AgentFailure as error:
            return Response({'error': str(error), 'has_context': False,
                             'retrieval': {'agent': error.trace}}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        context = retrieval['context']
        if agent.trace['stop_reason'] == 'deadline_exceeded':
            return Response({'error': '本次查询等待超时，请稍后重试。', 'has_context': False,
                             'retrieval': {k: v for k, v in retrieval.items() if k != 'context'}},
                            status=status.HTTP_504_GATEWAY_TIMEOUT)
        if retrieval.get('resume_token'):
            selected = '、'.join(retrieval['diseases'])
            answer = f'已查到关联资料，本次选定对象：{selected}（按名称排序，最多2个）。进度已保存，请在剩余时间内点击“继续整理”。'
        elif not context:
            answers = {'ambiguous': '问题涉及多个疾病，请明确希望查询哪一种。',
                       'nonmedical': '当前仅支持医疗知识资料查询，请输入相关问题。',
                       'unsupported_field': '当前图谱未收录经过核验的药品商品名、厂家或品牌资料，无法据此回答。'}
            answer = answers.get(retrieval['status'], '现有资料不足以完整回答这个问题。请补充具体疾病名称或缩小问题范围；不能据此提供诊断或用药建议。')
        else:
            answer = agent.answer(query, retrieval, conversation_history)
            if answer is None:
                timeout = agent.trace['stop_reason'] == 'deadline_exceeded'
                return Response({'error': '本次回答等待超时，请稍后重试。' if timeout else '回答服务暂时不可用，请稍后重试。',
                                 'has_context': True,
                                 'retrieval': {k: v for k, v in retrieval.items() if k != 'context'}},
                                status=status.HTTP_504_GATEWAY_TIMEOUT if timeout else status.HTTP_503_SERVICE_UNAVAILABLE)
            answer += source_appendix(retrieval['facts'])

        response_data = {
            "query": query,
            "answer": answer,
            "has_context": bool(context)
        }

        if retrieval is not None:
            response_data['retrieval'] = {key: value for key, value in retrieval.items() if key != 'context'}

        if conversation:
            user_message = ChatMessage.objects.create(
                conversation=conversation,
                role='user',
                content=query,
            )
            assistant_message = ChatMessage.objects.create(
                conversation=conversation,
                role='assistant',
                content=answer,
                has_context=bool(context),
            )
            if conversation.title == '新对话':
                conversation.title = f'{query[:30]}…' if len(query) > 30 else query
            conversation.save()
            response_data.update({
                'conversation_id': conversation.id,
                'conversation_title': conversation.title,
                'user_message': serialize_message(user_message),
                'assistant_message': serialize_message(assistant_message),
            })

        return Response(response_data)
