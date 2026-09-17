# apps/billing/views/hotspot_chat_admin_views.py
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import HasRoleAccessPolicy, IsAdminOrStaff
from apps.billing.models.hotspot_chat_models import HotspotChatThread, HotspotChatMessage
from apps.billing.views.hotspot_chat_views import _serialize_thread, _serialize_message


class HotspotChatThreadListView(APIView):
    """GET /api/v1/hotspot/admin/chats/  — reuses the tickets RBAC bucket."""
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/tickets"

    def get(self, request):
        status_filter = request.query_params.get('status')
        search = (request.query_params.get('search') or '').strip()

        qs = HotspotChatThread.objects.select_related('router', 'assigned_to')
        if status_filter and status_filter != 'all':
            qs = qs.filter(status=status_filter)
        if search:
            qs = qs.filter(phone_number__icontains=search)

        return Response({
            'count': qs.count(),
            'results': [_serialize_thread(t) for t in qs[:200]],
        })


class HotspotChatThreadDetailView(APIView):
    """
    GET  /api/v1/hotspot/admin/chats/<id>/           — full history, clears admin unread
    POST /api/v1/hotspot/admin/chats/<id>/reply/      — agent reply
    POST /api/v1/hotspot/admin/chats/<id>/status/     — update status
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/tickets"

    def get_object(self, pk):
        return HotspotChatThread.objects.select_related('router', 'assigned_to').filter(pk=pk).first()

    def get(self, request, pk):
        thread = self.get_object(pk)
        if not thread:
            return Response({'error': 'Not found'}, status=404)
        if thread.unread_by_admin:
            thread.unread_by_admin = False
            thread.save(update_fields=['unread_by_admin'])
        return Response(_serialize_thread(thread, with_messages=True))


class HotspotChatReplyView(APIView):
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/tickets"

    def post(self, request, pk):
        thread = HotspotChatThread.objects.filter(pk=pk).first()
        if not thread:
            return Response({'error': 'Not found'}, status=404)

        body = (request.data.get('message') or '').strip()
        if not body:
            return Response({'error': 'message is required'}, status=400)

        message = HotspotChatMessage.objects.create(
            thread=thread,
            sender_type='agent',
            agent=request.user,
            sender_name=request.user.get_full_name() or request.user.email or 'Support',
            body=body,
        )
        thread.unread_by_customer = True
        if not thread.assigned_to:
            thread.assigned_to = request.user
            thread.save(update_fields=['assigned_to'])
        thread.touch(message, status_value='pending')

        return Response(_serialize_message(message), status=status.HTTP_201_CREATED)


class HotspotChatStatusView(APIView):
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/tickets"

    def post(self, request, pk):
        thread = HotspotChatThread.objects.filter(pk=pk).first()
        if not thread:
            return Response({'error': 'Not found'}, status=404)
        new_status = request.data.get('status')
        if new_status not in dict(HotspotChatThread.STATUS_CHOICES):
            return Response({'error': 'Invalid status'}, status=400)
        thread.status = new_status
        thread.save(update_fields=['status', 'updated_at'])
        return Response({'id': thread.id, 'status': thread.status})