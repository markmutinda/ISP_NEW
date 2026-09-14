from django.utils import timezone
from django.db.models import Q
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.superadmin.permissions import IsSuperAdmin

from .models import SupportChatConversation, SupportChatMessage
from .support_knowledge import answer_support_question, support_chat_status


def _user_display(user):
    full_name = ""
    if user and getattr(user, "is_authenticated", False):
        full_name = (user.get_full_name() or "").strip()
    return full_name or getattr(user, "email", "") or getattr(user, "phone_number", "") or "User"


def _tenant_payload(request):
    tenant = getattr(request, "tenant", None)
    company = getattr(request, "company", None) or getattr(tenant, "company", None)
    return {
        "tenant_id": getattr(tenant, "id", None),
        "tenant_schema": getattr(tenant, "schema_name", "") or "",
        "tenant_name": getattr(company, "name", "") or getattr(tenant, "name", "") or getattr(tenant, "subdomain", ""),
        "tenant_subdomain": getattr(tenant, "subdomain", "") or "",
    }


def _serialize_message(message):
    return {
        "id": str(message.id),
        "conversation_id": str(message.conversation_id),
        "sender_type": message.sender_type,
        "sender_user_id": message.sender_user_id,
        "sender_name": message.sender_name,
        "sender_email": message.sender_email,
        "body": message.body,
        "read_at": message.read_at.isoformat() if message.read_at else None,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def _serialize_conversation(conversation, *, include_messages=False):
    payload = {
        "id": str(conversation.id),
        "tenant_id": str(conversation.tenant_id),
        "tenant_schema": conversation.tenant_schema,
        "tenant_name": conversation.tenant_name,
        "tenant_subdomain": conversation.tenant_subdomain,
        "category": conversation.category,
        "subject": conversation.subject,
        "status": conversation.status,
        "priority": conversation.priority,
        "created_by_name": conversation.created_by_name,
        "created_by_email": conversation.created_by_email,
        "created_by_phone": conversation.created_by_phone,
        "assigned_to_user_id": conversation.assigned_to_user_id,
        "assigned_to_name": conversation.assigned_to_name,
        "last_message_preview": conversation.last_message_preview,
        "last_message_at": conversation.last_message_at.isoformat() if conversation.last_message_at else None,
        "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
        "updated_at": conversation.updated_at.isoformat() if conversation.updated_at else None,
        "resolved_at": conversation.resolved_at.isoformat() if conversation.resolved_at else None,
    }
    if include_messages:
        payload["messages"] = [_serialize_message(message) for message in conversation.messages.all().order_by("created_at")]
    return payload


def _touch_conversation(conversation, message, *, status_value=None):
    conversation.last_message_preview = message.body[:260]
    conversation.last_message_at = message.created_at
    if status_value:
        conversation.status = status_value
    conversation.save(update_fields=["last_message_preview", "last_message_at", "status", "updated_at"])


class SupportChatDemoView(APIView):
    """
    Existing docs assistant endpoint kept for compatibility.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(support_chat_status())

    def post(self, request):
        question = request.data.get("message") or request.data.get("question") or ""
        result = answer_support_question(question)
        return Response(result)


class TenantSupportChatCurrentView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _tenant_payload(request)
        if not tenant["tenant_id"]:
            return Response({"detail": "Tenant context was not found."}, status=status.HTTP_400_BAD_REQUEST)

        with schema_context(get_public_schema_name()):
            conversation = (
                SupportChatConversation.objects
                .filter(tenant_id=tenant["tenant_id"])
                .exclude(status="resolved")
                .order_by("-last_message_at", "-created_at")
                .first()
            )
            if not conversation:
                return Response({"conversation": None, "messages": []})
            conversation.tenant_last_read_at = timezone.now()
            conversation.save(update_fields=["tenant_last_read_at", "updated_at"])
            return Response({
                "conversation": _serialize_conversation(conversation),
                "messages": [_serialize_message(message) for message in conversation.messages.all().order_by("created_at")],
            })

    def post(self, request):
        tenant = _tenant_payload(request)
        if not tenant["tenant_id"]:
            return Response({"detail": "Tenant context was not found."}, status=status.HTTP_400_BAD_REQUEST)

        body = (request.data.get("message") or "").strip()
        if not body:
            return Response({"detail": "Message is required."}, status=status.HTTP_400_BAD_REQUEST)

        category = (request.data.get("category") or "General").strip()[:80]
        subject = (request.data.get("subject") or category or "Support request").strip()[:180]
        user = request.user

        with schema_context(get_public_schema_name()):
            conversation = SupportChatConversation.objects.create(
                **tenant,
                category=category,
                subject=subject,
                status="new",
                priority=request.data.get("priority") or "normal",
                created_by_user_id=getattr(user, "id", None),
                created_by_name=_user_display(user),
                created_by_email=getattr(user, "email", "") or "",
                created_by_phone=getattr(user, "phone_number", "") or "",
            )
            message = SupportChatMessage.objects.create(
                conversation=conversation,
                sender_type="tenant",
                sender_user_id=getattr(user, "id", None),
                sender_name=_user_display(user),
                sender_email=getattr(user, "email", "") or "",
                body=body,
            )
            _touch_conversation(conversation, message, status_value="new")
            return Response({
                "conversation": _serialize_conversation(conversation),
                "messages": [_serialize_message(message)],
            }, status=status.HTTP_201_CREATED)


class TenantSupportChatMessagesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        tenant = _tenant_payload(request)
        with schema_context(get_public_schema_name()):
            conversation = SupportChatConversation.objects.filter(
                id=conversation_id,
                tenant_id=tenant["tenant_id"],
            ).first()
            if not conversation:
                return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)
            conversation.tenant_last_read_at = timezone.now()
            conversation.save(update_fields=["tenant_last_read_at", "updated_at"])
            return Response({
                "conversation": _serialize_conversation(conversation),
                "messages": [_serialize_message(message) for message in conversation.messages.all().order_by("created_at")],
            })

    def post(self, request, conversation_id):
        tenant = _tenant_payload(request)
        body = (request.data.get("message") or "").strip()
        if not body:
            return Response({"detail": "Message is required."}, status=status.HTTP_400_BAD_REQUEST)

        with schema_context(get_public_schema_name()):
            conversation = SupportChatConversation.objects.filter(
                id=conversation_id,
                tenant_id=tenant["tenant_id"],
            ).first()
            if not conversation:
                return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

            if conversation.status == "resolved":
                conversation.status = "open"
                conversation.resolved_at = None
                conversation.save(update_fields=["status", "resolved_at", "updated_at"])

            message = SupportChatMessage.objects.create(
                conversation=conversation,
                sender_type="tenant",
                sender_user_id=getattr(request.user, "id", None),
                sender_name=_user_display(request.user),
                sender_email=getattr(request.user, "email", "") or "",
                body=body,
            )
            _touch_conversation(conversation, message, status_value="open")
            return Response({
                "conversation": _serialize_conversation(conversation),
                "message": _serialize_message(message),
            }, status=status.HTTP_201_CREATED)


class SuperadminSupportChatConversationListView(APIView):
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request):
        status_filter = request.query_params.get("status")
        search = (request.query_params.get("search") or "").strip()
        page = max(1, int(request.query_params.get("page", 1)))
        page_size = min(100, max(1, int(request.query_params.get("page_size", 50))))

        with schema_context(get_public_schema_name()):
            qs = SupportChatConversation.objects.all()
            if status_filter:
                qs = qs.filter(status=status_filter)
            else:
                qs = qs.exclude(status="resolved")
            if search:
                qs = qs.filter(
                    Q(tenant_name__icontains=search) |
                    Q(tenant_subdomain__icontains=search) |
                    Q(subject__icontains=search) |
                    Q(last_message_preview__icontains=search)
                )
            total = qs.count()
            rows = qs.order_by("-last_message_at", "-created_at")[(page - 1) * page_size:page * page_size]
            return Response({
                "count": total,
                "page": page,
                "page_size": page_size,
                "results": [_serialize_conversation(row) for row in rows],
            })

    def post(self, request):
        return Response({"detail": "Create conversations from a tenant dashboard."}, status=status.HTTP_405_METHOD_NOT_ALLOWED)


class SuperadminSupportChatConversationDetailView(APIView):
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request, conversation_id):
        with schema_context(get_public_schema_name()):
            conversation = SupportChatConversation.objects.filter(id=conversation_id).first()
            if not conversation:
                return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)
            conversation.superadmin_last_read_at = timezone.now()
            conversation.save(update_fields=["superadmin_last_read_at", "updated_at"])
            return Response(_serialize_conversation(conversation, include_messages=True))

    def patch(self, request, conversation_id):
        allowed_statuses = {choice[0] for choice in SupportChatConversation.STATUS_CHOICES}
        with schema_context(get_public_schema_name()):
            conversation = SupportChatConversation.objects.filter(id=conversation_id).first()
            if not conversation:
                return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

            status_value = request.data.get("status")
            if status_value:
                if status_value not in allowed_statuses:
                    return Response({"detail": "Invalid status."}, status=status.HTTP_400_BAD_REQUEST)
                conversation.status = status_value
                conversation.resolved_at = timezone.now() if status_value == "resolved" else None

            priority = request.data.get("priority")
            if priority:
                conversation.priority = priority

            if request.data.get("assigned_to_me"):
                conversation.assigned_to_user_id = request.user.id
                conversation.assigned_to_name = _user_display(request.user)

            conversation.save()
            return Response(_serialize_conversation(conversation))

    def post(self, request, conversation_id):
        body = (request.data.get("message") or "").strip()
        if not body:
            return Response({"detail": "Message is required."}, status=status.HTTP_400_BAD_REQUEST)

        with schema_context(get_public_schema_name()):
            conversation = SupportChatConversation.objects.filter(id=conversation_id).first()
            if not conversation:
                return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)
            message = SupportChatMessage.objects.create(
                conversation=conversation,
                sender_type="superadmin",
                sender_user_id=request.user.id,
                sender_name=_user_display(request.user),
                sender_email=getattr(request.user, "email", "") or "",
                body=body,
            )
            _touch_conversation(conversation, message, status_value="waiting_on_tenant")
            return Response({
                "conversation": _serialize_conversation(conversation),
                "message": _serialize_message(message),
            }, status=status.HTTP_201_CREATED)
