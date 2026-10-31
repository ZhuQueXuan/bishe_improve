from django.urls import path
from .views import (RegisterView, LoginView, StatisticsView, KnowledgeQAView,
                    ConversationListCreateView, ConversationDetailView, DocumentFileView)
from .memory_views import LongMemoryView,ForgetMemoryView,ConversationMemoryView,GuestMemoryView,HealthProposalConfirmView,HealthMemoryChangeView,MemoryMaintenanceView
from .natural_memory_background_views import AvailabilityView,InboxStateView,SettingsView,DecisionView,RevisionView,EntryView

urlpatterns = [
    path('natural-memory/availability/',AvailabilityView.as_view()),
    path('natural-memory/inbox/',InboxStateView.as_view()),
    path('natural-memory/settings/',SettingsView.as_view()),
    path('natural-memory/decision/',DecisionView.as_view()),
    path('natural-memory/revision/',RevisionView.as_view()),
    path('natural-memory/entry/',EntryView.as_view()),
    path('memory/maintenance/',MemoryMaintenanceView.as_view(),name='memory_maintenance'),
    path('memory/health/proposals/<int:proposal_id>/confirm/',HealthProposalConfirmView.as_view(),name='confirm_health_memory'),
    path('memory/health/<int:memory_id>/',HealthMemoryChangeView.as_view(),name='change_health_memory'),
    path('memory/',LongMemoryView.as_view(),name='user_memory'),
    path('memory/<int:memory_id>/forget/',ForgetMemoryView.as_view(),name='forget_memory'),
    path('memory/preview/',GuestMemoryView.as_view(),name='guest_memory_preview'),
    path('conversations/<int:conversation_id>/memory/',ConversationMemoryView.as_view(),name='conversation_memory'),
    path('documents/<str:version_id>/file/', DocumentFileView.as_view(), name='document_file'),
    path('auth/register/', RegisterView.as_view(), name='register'),
    path('auth/login/', LoginView.as_view(), name='login'),
    path('conversations/', ConversationListCreateView.as_view(), name='conversation_list_create'),
    path('conversations/<int:conversation_id>/', ConversationDetailView.as_view(), name='conversation_detail'),
    path('statistics/', StatisticsView.as_view(), name='statistics'),
    path('ai/knowledge-qa/', KnowledgeQAView.as_view(), name='knowledge_qa'),
]
