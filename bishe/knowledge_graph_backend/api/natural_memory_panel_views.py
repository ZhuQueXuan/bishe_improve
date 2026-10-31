"""Mounted by the isolated URLconf only."""
from rest_framework.response import Response
from .natural_memory_views import IsolatedMemoryView
from .natural_memory_contract import NaturalMemoryContractError
from . import natural_memory_panel as panel


class PanelStateView(IsolatedMemoryView):
    def get(self, request):
        try:
            return Response(panel.read_panel(request.user))
        except NaturalMemoryContractError as error:
            return Response({'status': 'unavailable', 'reason': str(error)}, status=404 if str(error) == 'isolated_disabled' else 409)


class PanelSettingsView(IsolatedMemoryView):
    operation = staticmethod(panel.update_settings)


class PanelConversationView(IsolatedMemoryView):
    operation = staticmethod(panel.new_conversation)


class PanelMessageView(IsolatedMemoryView):
    operation = staticmethod(panel.process_message)
