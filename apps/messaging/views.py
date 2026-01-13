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

from .models import SMSMessage, SMSTemplate, SMSCampaign
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
)
from .services.sms_service import SMSService


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
        """Send single SMS via Africa's Talking"""
        sms_service = SMSService()
        sms_message = serializer.save(status='pending', type='single')

        result = sms_service.send_single(
            recipient=sms_message.recipient,
            message=sms_message.message,
            template=sms_message.template,
            customer=sms_message.customer,
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

        sms_service = SMSService()
        result = sms_service.send_bulk(
            recipients=serializer.validated_data['recipients'],
            message=serializer.validated_data.get('message'),
            template=serializer.validated_data.get('template'),
        )

        if not result.get('success', True):  # bulk can partially succeed
            return Response(
                {"detail": result.get('error', 'Bulk operation failed'), "details": result},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response({
            "detail": f"Queued {result['queued']} messages",
            "total_cost": result['total_cost'],
            "messages": result['messages']
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

        sms_service = SMSService()
        result = sms_service.send_single(
            recipient=sms_message.recipient,
            message=sms_message.message,
            template=sms_message.template,
            customer=sms_message.customer,
            campaign=sms_message.campaign,
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
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    def get(self, request):
        sms_service = SMSService()
        balance_info = sms_service.get_balance()

        # Add timestamp
        balance_info['last_updated'] = timezone.now().isoformat()

        return Response(SMSBalanceSerializer(balance_info).data)