"""R4 authenticated fixture endpoints. Not imported by production api.urls."""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .natural_memory_contract import NaturalMemoryContractError
from . import natural_memory_intake as intake
from .natural_memory_answering import post_question
from . import natural_memory_lifecycle as lifecycle


class IsolatedMemoryView(APIView):
    permission_classes = [IsAuthenticated]
    operation = None

    def post(self, request):
        try:
            result = self.operation(request.user, request.data)
            nested = result.get('answer_result', result)
            if nested.get('status') == 'answered' and nested.get('delivery_key'):
                checked = lifecycle.read_delivery(request.user, nested['delivery_key'])
                result = checked if nested is result else {**result, 'answer_status': checked['status'], 'answer_result': checked}
            return Response(result)
        except NaturalMemoryContractError as error:
            reason = str(error)
            status = 404 if reason == 'isolated_disabled' else 409 if reason.startswith(('stale_', 'write_conflict',
                'no_current_display', 'request_key_reused', 'clarification_', 'source_', 'intent_', 'newer_')) else 400
            return Response({'status': 'rejected', 'reason': reason, 'saved_ack': False, 'writes_performed': False}, status=status)
        except (TypeError, KeyError, AttributeError, ValueError):
            return Response({'status': 'rejected', 'reason': 'invalid_input', 'saved_ack': False,
                'writes_performed': False}, status=400)


class NaturalMessageView(IsolatedMemoryView):
    operation = staticmethod(intake.post_message)


class NaturalConfirmView(IsolatedMemoryView):
    operation = staticmethod(intake.confirm_display)


class NaturalManageView(IsolatedMemoryView):
    operation = staticmethod(intake.manage_entry)


class NaturalQuestionView(IsolatedMemoryView):
    operation = staticmethod(post_question)


class NaturalPausedQuestionView(IsolatedMemoryView):
    operation = staticmethod(lambda user, body: post_question(user, body, pause_before_answer=True))


class NaturalResumeView(IsolatedMemoryView):
    operation = staticmethod(lifecycle.resume)


class NaturalResetView(IsolatedMemoryView):
    operation = staticmethod(lifecycle.reset_conversation)


class NaturalDeliveryView(IsolatedMemoryView):
    def get(self, request, artifact_key):
        try:
            result = lifecycle.read_delivery(request.user, artifact_key)
            return Response(result, status=200 if result['status'] == 'answered' else 410)
        except NaturalMemoryContractError as error:
            return Response({'status': 'answer_unavailable', 'reason': str(error), 'answer': None}, status=409)


class NaturalStateView(IsolatedMemoryView):
    def get(self, request):
        try:
            return Response(intake.read_state(request.user))
        except NaturalMemoryContractError as error:
            return Response({'status': 'unavailable', 'reason': str(error), 'writes_performed': False},
                status=404 if str(error) == 'isolated_disabled' else 409)
