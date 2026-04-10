# apps/messaging/views.py
from decimal import Decimal
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.views import APIView
from django.db.models import Count, Sum, Q
from django.utils import timezone
from datetime import timedelta
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters

from .models import SMSMessage, SMSTemplate, SMSCampaign, SMSGatewayConfig
from .serializers import (
    SMSMessageSerializer,
    SMSMessageCreateSerializer,
    SMSBulkCreateSerializer,
    SMSTemplateSerializer,
    SMSTemplateCreateUpdateSerializer,
    SMSCampaignSerializer,
    SMSCampaignCreateUpdateSerializer,
    SMSStatsSerializer,
    SMSBalanceSerializer,
    SMSGatewayConfigSerializer,
    SMSGatewayConfigWriteSerializer,
)
from .services.gateway_dispatcher import GatewayDispatcher, PROVIDER_FIELDS


class SMSMessageViewSet(viewsets.ModelViewSet):
    """
    SMS Messages ViewSet
    Handles single send, bulk send, retry, list, retrieve
    """
    queryset = SMSMessage.objects.select_related('template', 'campaign', 'customer').order_by('-created_at')
    serializer_class = SMSMessageSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'type', 'provider', 'campaign__id']
    search_fields = ['recipient', 'message', 'recipient_name', 'error_message']
    ordering_fields = ['created_at', 'sent_at', 'status', 'cost']

    def get_serializer_class(self):
        if self.action == 'create':
            return SMSMessageCreateSerializer
        if self.action == 'bulk_send':
            return SMSBulkCreateSerializer
        return SMSMessageSerializer

    def perform_create(self, serializer):
        """Send single SMS via the active gateway provider"""
        sms_message = serializer.save(status='pending', type='single')

        try:
            dispatcher = GatewayDispatcher()
        except ValueError as e:
            sms_message.mark_failed(str(e))
            raise serializers.ValidationError({"send_error": str(e), "status": "failed"})

        result = dispatcher.send_sms(
            to=sms_message.recipient,
            message=sms_message.message,
        )

        if not result['success']:
            sms_message.mark_failed(result.get('error', 'Send failed'))
            raise serializers.ValidationError({
                "send_error": result.get('error', 'Failed to queue SMS'),
                "status": "failed"
            })

        # Update model with real data from provider
        sms_message.provider_message_id = result.get('provider_id')
        sms_message.cost = Decimal(str(result.get('cost', '0.00')))
        sms_message.status = result['status']
        sms_message.sent_at = timezone.now()
        sms_message.save(update_fields=['provider_message_id', 'cost', 'status', 'sent_at'])

    @action(detail=False, methods=['post'], url_path='bulk')
    def bulk_send(self, request):
        """Bulk SMS sending"""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            dispatcher = GatewayDispatcher()
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        recipients = serializer.validated_data['recipients']
        message = serializer.validated_data.get('message', '')
        if not message and serializer.validated_data.get('template'):
            message = serializer.validated_data['template'].content

        results = []
        total_cost = Decimal('0.00')
        for phone in recipients:
            r = dispatcher.send_sms(to=phone, message=message)
            sms_msg = SMSMessage.objects.create(
                recipient=phone,
                message=message,
                status=r.get('status', 'failed'),
                type='bulk',
                provider=dispatcher.config.provider,
                provider_message_id=r.get('provider_id', ''),
                cost=r.get('cost', Decimal('0.00')),
                sent_at=timezone.now() if r.get('success') else None,
                error_message=r.get('error', ''),
            )
            total_cost += sms_msg.cost
            results.append({'id': sms_msg.id, 'recipient': phone, 'status': sms_msg.status})

        return Response({
            "detail": f"Queued {len(recipients)} messages",
            "total_cost": str(total_cost),
            "messages": results,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='retry')
    def retry(self, request, pk=None):
        """Retry a failed message"""
        sms_message = self.get_object()

        if sms_message.status != 'failed':
            return Response(
                {"detail": f"Cannot retry message in status '{sms_message.status}'"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            dispatcher = GatewayDispatcher()
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        result = dispatcher.send_sms(
            to=sms_message.recipient,
            message=sms_message.message,
        )

        if result['success']:
            sms_message.provider_message_id = result.get('provider_id')
            sms_message.cost = Decimal(str(result.get('cost', '0.00')))
            sms_message.status = 'sent'
            sms_message.sent_at = timezone.now()
            sms_message.error_message = None
            sms_message.save(update_fields=[
                'provider_message_id', 'cost', 'status', 'sent_at', 'error_message'
            ])
            return Response({
                "detail": "Retry successful",
                "new_status": "sent",
                "message_id": sms_message.id,
                "cost": sms_message.cost
            }, status=status.HTTP_200_OK)
        else:
            sms_message.mark_failed(result.get('error', 'Retry failed'))
            return Response({
                "detail": "Retry failed",
                "error": result.get('error')
            }, status=status.HTTP_400_BAD_REQUEST)


class SMSTemplateViewSet(viewsets.ModelViewSet):
    queryset = SMSTemplate.objects.order_by('-created_at')
    serializer_class = SMSTemplateSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'content']

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return SMSTemplateCreateUpdateSerializer
        return SMSTemplateSerializer


class SMSCampaignViewSet(viewsets.ModelViewSet):
    queryset = SMSCampaign.objects.order_by('-created_at')
    serializer_class = SMSCampaignSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['status']
    search_fields = ['name']

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return SMSCampaignCreateUpdateSerializer
        return SMSCampaignSerializer

    @action(detail=True, methods=['post'], url_path='start')
    def start(self, request, pk=None):
        campaign = self.get_object()

        if campaign.status not in ['draft', 'scheduled']:
            return Response(
                {"detail": f"Cannot start campaign in status '{campaign.status}'"},
                status=status.HTTP_400_BAD_REQUEST
            )

        campaign.status = 'running'
        campaign.started_at = timezone.now()
        campaign.save(update_fields=['status', 'started_at'])

        # TODO: Trigger Celery task here in production
        # from .tasks import process_sms_campaign
        # process_sms_campaign.delay(campaign.id)

        return Response({
            "detail": "Campaign started",
            "status": campaign.status,
            "started_at": campaign.started_at
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        campaign = self.get_object()

        if campaign.status not in ['running', 'scheduled']:
            return Response(
                {"detail": f"Cannot cancel campaign in status '{campaign.status}'"},
                status=status.HTTP_400_BAD_REQUEST
            )

        campaign.status = 'cancelled'
        campaign.save(update_fields=['status'])

        # TODO: revoke Celery tasks if needed

        return Response({
            "detail": "Campaign cancelled",
            "status": campaign.status
        }, status=status.HTTP_200_OK)


# ────────────────────────────────────────────────
# Stats & Balance – using APIView (no .as_view(actions) needed)
# ────────────────────────────────────────────────

class SMSStatsView(APIView):
    """
    GET /api/v1/messaging/sms/stats/
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today - timedelta(days=today.weekday())

        qs = SMSMessage.objects.all()

        agg = qs.aggregate(
            total_sent=Count('id'),
            delivered=Count('id', filter=Q(status='delivered')),
            pending=Count('id', filter=Q(status='pending')),
            failed=Count('id', filter=Q(status='failed')),
            total_cost=Sum('cost'),
            today_count=Count('id', filter=Q(created_at__gte=today)),
            week_count=Count('id', filter=Q(created_at__gte=week_start)),
        )

        delivered = agg['delivered'] or 0
        total = agg['total_sent'] or 0
        delivery_rate = round((delivered / total * 100) if total > 0 else 0, 1)

        data = {
            'total_sent': agg['total_sent'] or 0,
            'delivered': delivered,
            'pending': agg['pending'] or 0,
            'failed': agg['failed'] or 0,
            'delivery_rate': delivery_rate,
            'total_cost': agg['total_cost'] or Decimal('0.00'),
            'messages_today': agg['today_count'] or 0,
            'messages_this_week': agg['week_count'] or 0,
        }

        return Response(SMSStatsSerializer(data).data)


class SMSBalanceView(APIView):
    """
    GET /api/v1/messaging/sms/balance/
    Uses the active gateway's provider SDK to fetch real balance.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        try:
            dispatcher = GatewayDispatcher()
            balance_info = dispatcher.get_balance()
        except ValueError:
            balance_info = {'balance': 0, 'currency': 'KES', 'success': False}

        balance_info.setdefault('unit_cost', 0.50)
        balance_info.setdefault('provider', 'none')
        bal = balance_info.get('balance', 0) or 0
        unit_cost = balance_info.get('unit_cost', 0.50) or 0.50
        balance_info['units_remaining'] = int(bal / unit_cost) if unit_cost else 0
        balance_info['last_updated'] = timezone.now().isoformat()

        return Response(SMSBalanceSerializer(balance_info).data)


# ────────────────────────────────────────────────
# Gateway Config CRUD + test connection
# ────────────────────────────────────────────────

class SMSGatewayConfigViewSet(viewsets.ModelViewSet):
    """
    /api/v1/messaging/gateway/
    CRUD for per-tenant SMS gateway configuration.
    """
    queryset = SMSGatewayConfig.objects.order_by('-is_active', '-updated_at')
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return SMSGatewayConfigWriteSerializer
        return SMSGatewayConfigSerializer

    @action(detail=True, methods=['post'], url_path='activate')
    def activate(self, request, pk=None):
        """Set this config as the active gateway, deactivate others."""
        config = self.get_object()
        SMSGatewayConfig.objects.exclude(pk=config.pk).update(is_active=False)
        config.is_active = True
        config.save(update_fields=['is_active'])
        return Response(SMSGatewayConfigSerializer(config).data)

    @action(detail=True, methods=['post'], url_path='test')
    def test_connection(self, request, pk=None):
        """Test the provider credentials by fetching balance."""
        config = self.get_object()
        from .services.gateway_dispatcher import BACKENDS
        cls = BACKENDS.get(config.provider)
        if not cls:
            return Response({"success": False, "error": "Unknown provider"}, status=400)
        try:
            backend = cls(
                api_key=config.api_key,
                api_secret=config.api_secret,
                username=config.username,
                sender_id=config.sender_id,
                extra_config=config.extra_config,
            )
            bal = backend.get_balance()
            return Response({"success": True, "balance": bal})
        except Exception as e:
            return Response({"success": False, "error": str(e)}, status=400)

    @action(detail=False, methods=['get'], url_path='providers')
    def list_providers(self, request):
        """List available providers and their required fields."""
        return Response(PROVIDER_FIELDS)
