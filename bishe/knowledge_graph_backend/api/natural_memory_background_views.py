"""Normal authenticated management routes; imports never start workers."""
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from . import natural_memory_background as background
from .natural_memory_contract import NaturalMemoryContractError

class AvailabilityView(APIView):
    permission_classes=[IsAuthenticated]
    def get(self,request):return Response({'available':background.available()})

class BackgroundView(APIView):
    permission_classes=[IsAuthenticated]
    operation=None
    def post(self,request):
        try:return Response(self.operation(request.user,request.data))
        except NaturalMemoryContractError as error:
            return Response({'status':'rejected','reason':str(error),'saved_ack':False},status=404 if str(error) in ['natural_memory_disabled','isolated_disabled'] else 409)
        except (KeyError,TypeError,ValueError):return Response({'status':'rejected','reason':'invalid_input','saved_ack':False},status=400)

class InboxStateView(BackgroundView):
    def get(self,request):
        try:return Response(background.read_state(request.user))
        except NaturalMemoryContractError as error:return Response({'status':'unavailable','reason':str(error)},status=404 if str(error) in ['natural_memory_disabled','isolated_disabled'] else 409)
class SettingsView(BackgroundView):operation=staticmethod(background.update_settings)
class DecisionView(BackgroundView):operation=staticmethod(background.change_item)
class RevisionView(BackgroundView):operation=staticmethod(background.revise_item)

class EntryView(BackgroundView):
    def post(self,request):
        try:
            background.enabled(request.user)
            from .natural_memory_intake import manage_entry
            return Response(manage_entry(request.user,request.data))
        except NaturalMemoryContractError as error:return Response({'status':'rejected','reason':str(error),'saved_ack':False},status=409)
