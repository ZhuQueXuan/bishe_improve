"""Explicit isolated ROOT_URLCONF for tests only; no production include."""
from django.urls import path
from .natural_memory_views import NaturalMessageView, NaturalConfirmView, NaturalManageView, NaturalStateView, NaturalQuestionView
from .natural_memory_views import NaturalPausedQuestionView, NaturalResumeView, NaturalResetView, NaturalDeliveryView
from .natural_memory_panel_views import PanelStateView, PanelSettingsView, PanelConversationView, PanelMessageView

urlpatterns = [
    path('natural-memory/panel/', PanelStateView.as_view()),
    path('natural-memory/panel/settings/', PanelSettingsView.as_view()),
    path('natural-memory/panel/conversations/', PanelConversationView.as_view()),
    path('natural-memory/panel/messages/', PanelMessageView.as_view()),
    path('natural-memory/questions/pause/', NaturalPausedQuestionView.as_view()),
    path('natural-memory/resume/', NaturalResumeView.as_view()),
    path('natural-memory/reset/', NaturalResetView.as_view()),
    path('natural-memory/results/<str:artifact_key>/', NaturalDeliveryView.as_view()),
    path('natural-memory/questions/', NaturalQuestionView.as_view()),
    path('natural-memory/messages/', NaturalMessageView.as_view()),
    path('natural-memory/confirm/', NaturalConfirmView.as_view()),
    path('natural-memory/manage/', NaturalManageView.as_view()),
    path('natural-memory/state/', NaturalStateView.as_view()),
]
