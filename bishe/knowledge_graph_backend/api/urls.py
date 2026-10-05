from django.urls import path
from .views import (RegisterView, LoginView, StatisticsView, KnowledgeQAView,
                    ConversationListCreateView, ConversationDetailView)

urlpatterns = [
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/login/', LoginView.as_view(), name='login'),
    path('conversations/', ConversationListCreateView.as_view(), name='conversation_list_create'),
    path('conversations/<int:conversation_id>/', ConversationDetailView.as_view(), name='conversation_detail'),
    path('statistics/', StatisticsView.as_view(), name='statistics'),
    path('ai/knowledge-qa/', KnowledgeQAView.as_view(), name='knowledge_qa'),
]
