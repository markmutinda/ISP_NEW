from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .support_knowledge import answer_support_question, support_chat_status


class SupportChatDemoView(APIView):
    """
    Safe tenant support assistant demo.

    Answers only from curated support docs under rag/netily-support and blocks
    architecture/deployment/credential questions.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(support_chat_status())

    def post(self, request):
        question = request.data.get("message") or request.data.get("question") or ""
        result = answer_support_question(question)
        return Response(result)
