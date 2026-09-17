# apps/billing/views/hotspot_chat_views.py
import logging
from django.db.models import Q
from django.utils import timezone
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.core.models import Tenant
from apps.billing.models.hotspot_chat_models import HotspotChatThread, HotspotChatMessage

logger = logging.getLogger(__name__)


class HotspotChatRateThrottle(AnonRateThrottle):
    scope = 'hotspot_chat'


def _resolve_tenant(tenant_value):
    if not tenant_value:
        return None
    with schema_context(get_public_schema_name()):
        return Tenant.objects.filter(
            Q(subdomain=tenant_value) | Q(schema_name=tenant_value),
            is_active=True,
        ).first()


def _canonical_phone(phone, tenant):
    from utils.phone import normalize_phone_number
    country = 'KE'
    try:
        country = tenant.company.country or 'KE'
    except Exception:
        pass
    return normalize_phone_number(phone, country)


def _serialize_message(m):
    return {
        'id': m.id,
        'sender_type': m.sender_type,
        'sender_name': m.sender_name,
        'body': m.body,
        'created_at': m.created_at.isoformat(),
    }


def _serialize_thread(t, *, with_messages=False):
    data = {
        'id': t.id,
        'phone_number': t.phone_number,
        'status': t.status,
        'subject': t.subject,
        'last_message_preview': t.last_message_preview,
        'last_message_at': t.last_message_at.isoformat() if t.last_message_at else None,
        'unread_by_customer': t.unread_by_customer,
    }
    if with_messages:
        data['messages'] = [_serialize_message(m) for m in t.messages.all()]
    return data


class HotspotChatInitView(APIView):
    """
    POST /api/v1/hotspot/chat/init/
    { tenant, phone_number, router_id? }

    Resolves (or creates) the thread for this phone number and returns
    its full history. This is the "re-enter your number to resume" endpoint.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [HotspotChatRateThrottle]

    def post(self, request):
        tenant_subdomain = request.data.get('tenant')
        raw_phone = (request.data.get('phone_number') or '').strip()
        router_id = request.data.get('router_id')

        if not tenant_subdomain or not raw_phone:
            return Response({'error': 'tenant and phone_number are required'}, status=400)

        tenant = _resolve_tenant(tenant_subdomain)
        if not tenant:
            return Response({'error': 'Invalid tenant'}, status=400)

        with schema_context(tenant.schema_name):
            phone = _canonical_phone(raw_phone, tenant)
            if not phone:
                return Response({'error': 'Invalid phone number'}, status=400)

            router = None
            if router_id:
                from apps.network.models.router_models import Router
                router = Router.objects.filter(id=router_id, is_active=True).first()

            thread, created = HotspotChatThread.objects.get_or_create(
                phone_number=phone,
                defaults={'router': router},
            )
            if not created and router and not thread.router:
                thread.router = router
                thread.save(update_fields=['router'])

            if thread.unread_by_customer:
                thread.unread_by_customer = False
                thread.save(update_fields=['unread_by_customer'])

            return Response({
                'thread': _serialize_thread(thread, with_messages=True),
                'is_new': created,
            }, status=status.HTTP_200_OK)


class HotspotChatSendView(APIView):
    """
    POST /api/v1/hotspot/chat/send/
    { tenant, phone_number, message, router_id? }
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [HotspotChatRateThrottle]

    def post(self, request):
        tenant_subdomain = request.data.get('tenant')
        raw_phone = (request.data.get('phone_number') or '').strip()
        body = (request.data.get('message') or '').strip()
        router_id = request.data.get('router_id')

        if not tenant_subdomain or not raw_phone or not body:
            return Response({'error': 'tenant, phone_number and message are required'}, status=400)
        if len(body) > 2000:
            return Response({'error': 'Message too long'}, status=400)

        tenant = _resolve_tenant(tenant_subdomain)
        if not tenant:
            return Response({'error': 'Invalid tenant'}, status=400)

        with schema_context(tenant.schema_name):
            phone = _canonical_phone(raw_phone, tenant)
            if not phone:
                return Response({'error': 'Invalid phone number'}, status=400)

            router = None
            if router_id:
                from apps.network.models.router_models import Router
                router = Router.objects.filter(id=router_id, is_active=True).first()

            thread, _created = HotspotChatThread.objects.get_or_create(
                phone_number=phone,
                defaults={'router': router},
            )

            message = HotspotChatMessage.objects.create(
                thread=thread,
                sender_type='customer',
                sender_name=phone,
                body=body,
            )
            # Re-opens a resolved thread the moment the customer writes again.
            thread.unread_by_admin = True
            thread.touch(message, status_value='open' if thread.status == 'resolved' else thread.status)

            return Response({
                'thread': _serialize_thread(thread, with_messages=False),
                'message': _serialize_message(message),
            }, status=status.HTTP_201_CREATED)