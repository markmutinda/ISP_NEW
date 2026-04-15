# apps/messaging/views.py
from decimal import Decimal
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from django.db import transaction
from django.db.models import Count, Sum, Q
from django.utils import timezone
from datetime import timedelta
from rest_framework import serializers
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
import logging

logger = logging.getLogger(__name__)

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
from .services.credit_billing_service import CreditBillingService

# Import custom permission
from apps.core.permissions import IsAdminOrStaff


class SMSMessageViewSet(viewsets.ModelViewSet):
    """
    SMS Messages ViewSet
    Handles single send, bulk send, retry, list, retrieve
    """
    queryset = SMSMessage.objects.select_related('template', 'campaign', 'customer').order_by('-created_at')
    serializer_class = SMSMessageSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
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
        """Send single SMS via the active gateway provider with wallet debit"""
        with transaction.atomic():
            sms_message = serializer.save(status='pending', type='single')

            # debit internal tenant wallet first
            debited_units = CreditBillingService.debit_for_sms(
                message_text=sms_message.message,
                sms_message=sms_message
            )

            try:
                dispatcher = GatewayDispatcher()
            except ValueError as e:
                CreditBillingService.refund_units(debited_units, sms_message=sms_message, notes=str(e))
                sms_message.mark_failed(str(e))
                raise serializers.ValidationError({"send_error": str(e), "status": "failed"})

            try:
                result = dispatcher.send_sms(
                    to=sms_message.recipient,
                    message=sms_message.message,
                )
            except Exception as e:
                CreditBillingService.refund_units(debited_units, sms_message=sms_message, notes=str(e))
                sms_message.mark_failed(str(e))
                raise serializers.ValidationError({"send_error": str(e), "status": "failed"})

            if not result['success']:
                CreditBillingService.refund_units(
                    debited_units, sms_message=sms_message, notes=result.get('error', 'Send failed')
                )
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
            sms_message.provider = dispatcher.config.provider
            sms_message.save(update_fields=['provider_message_id', 'cost', 'status', 'sent_at', 'provider'])

    @action(detail=False, methods=['post'], url_path='bulk')
    def bulk_send(self, request):
        """Bulk SMS sending with wallet debit for each message"""
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
            with transaction.atomic():
                sms_msg = SMSMessage.objects.create(
                    recipient=phone,
                    message=message,
                    status='pending',
                    type='bulk',
                )

                # debit internal tenant wallet first
                debited_units = CreditBillingService.debit_for_sms(
                    message_text=sms_msg.message,
                    sms_message=sms_msg
                )

                try:
                    r = dispatcher.send_sms(to=phone, message=message)
                except Exception as e:
                    CreditBillingService.refund_units(debited_units, sms_message=sms_msg, notes=str(e))
                    sms_msg.mark_failed(str(e))
                    results.append({
                        'id': sms_msg.id,
                        'recipient': phone,
                        'status': 'failed',
                        'error': str(e)
                    })
                    continue

                if not r.get('success'):
                    CreditBillingService.refund_units(
                        debited_units, sms_message=sms_msg, notes=r.get('error', 'Send failed')
                    )
                    sms_msg.mark_failed(r.get('error', 'Send failed'))
                    results.append({
                        'id': sms_msg.id,
                        'recipient': phone,
                        'status': 'failed',
                        'error': r.get('error', 'Send failed')
                    })
                else:
                    sms_msg.provider_message_id = r.get('provider_id', '')
                    sms_msg.cost = Decimal(str(r.get('cost', '0.00')))
                    sms_msg.status = 'sent'
                    sms_msg.sent_at = timezone.now()
                    sms_msg.provider = dispatcher.config.provider
                    sms_msg.save(update_fields=['provider_message_id', 'cost', 'status', 'sent_at', 'provider'])
                    total_cost += sms_msg.cost
                    results.append({
                        'id': sms_msg.id,
                        'recipient': phone,
                        'status': 'sent',
                        'cost': str(sms_msg.cost)
                    })

        return Response({
            "detail": f"Processed {len(recipients)} messages",
            "total_cost": str(total_cost),
            "messages": results,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'], url_path='retry')
    def retry(self, request, pk=None):
        """Retry a failed message with wallet debit"""
        sms_message = self.get_object()

        if sms_message.status != 'failed':
            return Response(
                {"detail": f"Cannot retry message in status '{sms_message.status}'"},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            # debit internal tenant wallet first
            debited_units = CreditBillingService.debit_for_sms(
                message_text=sms_message.message,
                sms_message=sms_message
            )

            try:
                dispatcher = GatewayDispatcher()
            except ValueError as e:
                CreditBillingService.refund_units(debited_units, sms_message=sms_message, notes=str(e))
                return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

            try:
                result = dispatcher.send_sms(
                    to=sms_message.recipient,
                    message=sms_message.message,
                )
            except Exception as e:
                CreditBillingService.refund_units(debited_units, sms_message=sms_message, notes=str(e))
                sms_message.mark_failed(str(e))
                return Response({
                    "detail": "Retry failed",
                    "error": str(e)
                }, status=status.HTTP_400_BAD_REQUEST)

            if result['success']:
                sms_message.provider_message_id = result.get('provider_id')
                sms_message.cost = Decimal(str(result.get('cost', '0.00')))
                sms_message.status = 'sent'
                sms_message.sent_at = timezone.now()
                sms_message.error_message = None
                sms_message.provider = dispatcher.config.provider
                sms_message.save(update_fields=[
                    'provider_message_id', 'cost', 'status', 'sent_at', 'error_message', 'provider'
                ])
                return Response({
                    "detail": "Retry successful",
                    "new_status": "sent",
                    "message_id": sms_message.id,
                    "cost": sms_message.cost
                }, status=status.HTTP_200_OK)
            else:
                CreditBillingService.refund_units(
                    debited_units, sms_message=sms_message, notes=result.get('error', 'Send failed')
                )
                sms_message.mark_failed(result.get('error', 'Retry failed'))
                return Response({
                    "detail": "Retry failed",
                    "error": result.get('error')
                }, status=status.HTTP_400_BAD_REQUEST)


class SMSTemplateViewSet(viewsets.ModelViewSet):
    queryset = SMSTemplate.objects.order_by('-created_at')
    serializer_class = SMSTemplateSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name', 'content']

    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return SMSTemplateCreateUpdateSerializer
        return SMSTemplateSerializer

    def list(self, request, *args, **kwargs):
        """Auto-seed default templates on first view."""
        # Auto-seed on first view
        if not SMSTemplate.objects.filter(is_system=True).exists():
            from apps.messaging.template_defaults import seed_default_templates
            seed_default_templates()
        return super().list(request, *args, **kwargs)


class SMSCampaignViewSet(viewsets.ModelViewSet):
    queryset = SMSCampaign.objects.order_by('-created_at')
    serializer_class = SMSCampaignSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
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

    # FIX 5: Campaign bulk SMS — send to group
    @action(detail=False, methods=['post'], url_path='send-to-group')
    def send_to_group(self, request):
        """
        Send a bulk SMS to all customers of a given type.
        Body: { "name": "May Promo", "message": "...", "group": "pppoe|hotspot|all" }
        """
        group = request.data.get('group', 'all')
        message = (request.data.get('message') or '').strip()
        name = request.data.get('name') or f'Bulk {group} — {timezone.now().strftime("%d %b %Y")}'

        if not message:
            return Response({'error': 'message is required'}, status=status.HTTP_400_BAD_REQUEST)
        if group not in ('pppoe', 'hotspot', 'all'):
            return Response({'error': 'group must be pppoe, hotspot, or all'}, status=status.HTTP_400_BAD_REQUEST)

        phones = set()

        if group in ('pppoe', 'all'):
            from apps.customers.models import Customer
            for phone in Customer.objects.filter(
                status='ACTIVE'
            ).values_list('user__phone_number', flat=True):
                if phone:
                    phones.add(phone)

        if group in ('hotspot', 'all'):
            from apps.billing.models.hotspot_models import HotspotClient
            for phone in HotspotClient.objects.filter(
                canonical_phone__isnull=False
            ).exclude(
                canonical_phone__startswith='MAC-'
            ).values_list('canonical_phone', flat=True):
                if phone:
                    phones.add(phone)

        phones = list(phones)
        campaign = SMSCampaign.objects.create(
            name=name,
            message=message,
            recipient_count=len(phones),
            recipient_filter={'group': group},
            status='running',
            started_at=timezone.now(),
        )

        from apps.messaging.tasks import process_campaign_sms
        process_campaign_sms.delay(campaign.id, phones, message)

        return Response({
            'campaign_id': campaign.id,
            'name': name,
            'group': group,
            'recipient_count': len(phones),
            'message': 'Campaign is being sent in the background.',
        }, status=status.HTTP_202_ACCEPTED)


# ────────────────────────────────────────────────
# Stats & Balance – using APIView (no .as_view(actions) needed)
# ────────────────────────────────────────────────

class SMSStatsView(APIView):
    """
    GET /api/v1/messaging/sms/stats/
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

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
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

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
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

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
        
        # Seed templates if first time
        from apps.messaging.template_defaults import seed_default_templates
        seed_default_templates()
        
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


# ============================================================
# NEW VIEWS ADDED BELOW
# ============================================================

from decimal import Decimal as _Decimal
from .models import SMSNotificationSettings, SMSUnitTopup, TenantSMSWallet
from .serializers import (
    SMSNotificationSettingsSerializer,
    SMSUnitTopupSerializer,
    SMSWalletSerializer,
)


class SMSNotificationSettingsView(APIView):
    """
    GET  /api/v1/messaging/notification-settings/
    PATCH /api/v1/messaging/notification-settings/
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        settings_obj = SMSNotificationSettings.get_settings()
        return Response(SMSNotificationSettingsSerializer(settings_obj).data)

    def patch(self, request):
        settings_obj = SMSNotificationSettings.get_settings()
        serializer = SMSNotificationSettingsSerializer(
            settings_obj, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # ── KEY FIX: keep SMSGatewayConfig in sync ────────────────────────
        if 'use_inbuilt_system' in request.data:
            use_inbuilt = request.data['use_inbuilt_system']
            if use_inbuilt:
                # Create or update the gateway config to use inbuilt
                gateway, created = SMSGatewayConfig.objects.get_or_create(
                    use_inbuilt_system=True,
                    defaults={
                        'provider': 'bytewave',
                        'is_active': True,
                        'api_key': '',
                        'api_secret': '',
                        'sender_id': '',
                    }
                )
                if not created:
                    # Deactivate all others and activate this one
                    SMSGatewayConfig.objects.exclude(pk=gateway.pk).update(is_active=False)
                    gateway.is_active = True
                    gateway.save(update_fields=['is_active'])
                else:
                    SMSGatewayConfig.objects.exclude(pk=gateway.pk).update(is_active=False)
            else:
                # When turning off inbuilt, just deactivate the inbuilt gateway.
                # The tenant will need to configure their own provider.
                SMSGatewayConfig.objects.filter(
                    use_inbuilt_system=True
                ).update(is_active=False)

        return Response(serializer.data)


class SMSWalletView(APIView):
    """
    GET /api/v1/messaging/wallet/
    Returns current balance + last 20 topup records.
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        wallet = TenantSMSWallet.objects.filter(is_active=True).first()
        topups = SMSUnitTopup.objects.order_by('-created_at')[:20]
        data = {
            'sms_units': wallet.sms_units if wallet else _Decimal('0'),
            'sell_price_per_unit': wallet.sell_price_per_unit if wallet else _Decimal('0.60'),
            'topup_history': SMSUnitTopupSerializer(topups, many=True).data,
        }
        return Response(data)


class SMSTopupInitiateView(APIView):
    """
    POST /api/v1/messaging/topup/initiate/
    Body: { "units": 1000, "phone_number": "254712345678" } 
           OR { "amount_kes": 100, "phone_number": "254712345678" }

    Calculates cost using tiered pricing, creates a pending topup record, initiates STK push.
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    # FIX 7: Tiered pricing
    TIERS = [
        (1000, Decimal('0.30')),   # 1000+ units
        (500,  Decimal('0.35')),   # 500–999 units
        (25,   Decimal('0.40')),   # 25–499 units
    ]
    MIN_AMOUNT_KES = Decimal('10')

    def _price_for(self, units: int) -> Decimal:
        """Get price per unit based on tiered pricing."""
        for threshold, price in self.TIERS:
            if units >= threshold:
                return price
        return Decimal('0.40')

    def post(self, request):
        phone = request.data.get('phone_number', '').strip()

        # Support both units-based and amount-based topup
        units = request.data.get('units')
        amount_kes = request.data.get('amount_kes')

        if amount_kes:
            # Calculate units from KES amount (use 0.40 base rate for custom amounts)
            try:
                amount_kes = Decimal(str(amount_kes)).quantize(Decimal('0.01'))
            except Exception:
                return Response({'error': 'Invalid amount_kes'}, status=status.HTTP_400_BAD_REQUEST)
            
            if amount_kes < self.MIN_AMOUNT_KES:
                return Response(
                    {'error': f'Minimum amount is KES {self.MIN_AMOUNT_KES}'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            # Calculate units at 0.40/unit (base rate) for custom amount
            units = int(amount_kes / Decimal('0.40'))
            if units < 25:
                units = 25
            price_per_unit = self._price_for(units)
            total_amount = (price_per_unit * units).quantize(Decimal('0.01'))
        elif units:
            try:
                units = int(units)
            except (ValueError, TypeError):
                return Response({'error': 'Invalid units'}, status=status.HTTP_400_BAD_REQUEST)
            
            if units < 25:
                return Response(
                    {'error': 'Minimum top-up is 25 units (KES 10)'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            price_per_unit = self._price_for(units)
            total_amount = (price_per_unit * units).quantize(Decimal('0.01'))
        else:
            return Response(
                {'error': 'Provide either units or amount_kes'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not phone:
            return Response(
                {'error': 'phone_number is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ─────────────────────────────────────────────────────────────────────
        # PERMANENT FIX: Use request.tenant (reliable) instead of connection
        # ─────────────────────────────────────────────────────────────────────
        from django.db import connection as _conn
        
        # Reliably get the tenant schema (fallback to connection if needed)
        current_schema = request.tenant.schema_name if hasattr(request, 'tenant') and request.tenant else _conn.schema_name

        # Reject top-ups that accidentally hit the public portal
        if current_schema in ('public', None, ''):
            return Response(
                {'error': 'Cannot initiate top-up from the public portal. Please do this from your tenant dashboard.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # OPTIONAL HARDENING: Validate schema exists and is active in public Tenant table
        try:
            from apps.core.models import Tenant
            from django_tenants.utils import schema_context, get_public_schema_name
            with schema_context(get_public_schema_name()):
                tenant_exists = Tenant.objects.filter(
                    schema_name=current_schema,
                    is_active=True
                ).exists()
                if not tenant_exists:
                    logger.warning(f"SMSTopupInitiateView: schema '{current_schema}' is not an active tenant")
                    return Response(
                        {'error': 'Invalid tenant schema. Please contact support.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
        except Exception as e:
            logger.error(f"SMSTopupInitiateView: tenant validation failed for {current_schema}: {e}")
            # Continue anyway - don't block on validation error, but log it

        topup = SMSUnitTopup.objects.create(
            units_purchased=units,
            amount_paid=total_amount,
            payment_method='mpesa_stk',
            status='pending',
            schema_name=current_schema,  # ← Uses the reliable schema from request.tenant
        )

        # ─────────────────────────────────────────────────────────────────────
        # FIX: Use Master Tuma Token (platform's account, not tenant's)
        # This ensures SMS top-up payments go to the platform's bank/paybill
        # instead of the tenant's individual account.
        # ─────────────────────────────────────────────────────────────────────
        try:
            from django.conf import settings as _settings
            from apps.billing.services.tuma_service import TumaClient

            # Instantiate the Tuma client
            client = TumaClient()
            
            # --- USE MASTER TOKEN (platform's own Tuma account) ---
            try:
                token = client.get_master_token()
            except Exception as e:
                logger.error(f"Failed to get master token for SMS topup: {e}")
                topup.status = 'failed'
                topup.notes = 'Platform payment gateway error'
                topup.save()
                return Response(
                    {'error': 'Platform payment gateway not configured correctly'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            # FIX 1: Use dedicated SMS topup callback URL
            # Route SMS-topup callbacks to the dedicated handler instead of the generic
            # TumaWebhookView, which can fail silently because it looks for a Payment row
            # that doesn't exist for SMS topups.
            base_url = getattr(_settings, 'BASE_URL', '').rstrip('/')
            if base_url:
                callback_url = f"{base_url}/api/v1/messaging/topup/callback/"
            else:
                callback_url = getattr(_settings, 'TUMA_CALLBACK_URL', '')
            desc = f"SMS-UNITS-{topup.id}"

            res = client.stk_push(
                token=token,  # This now uses the master token
                amount=float(total_amount),
                phone=phone,
                callback_url=callback_url,
                description=desc,
            )

            if res.get('success'):
                d = res.get('data', {})
                topup.checkout_request_id = d.get('checkout_request_id', '')
                topup.payment_reference = d.get('merchant_request_id', '')
                topup.save()
                
                # Register for fast callback lookup
                try:
                    from apps.core.models import TumaCallbackMap
                    from django_tenants.utils import schema_context, get_public_schema_name
                    with schema_context(get_public_schema_name()):
                        TumaCallbackMap.objects.update_or_create(
                            checkout_request_id=topup.checkout_request_id,
                            defaults={
                                'merchant_request_id': topup.payment_reference,
                                'schema_name': current_schema,  # ← Ensure accurate schema is saved here
                                'payment_reference': f'SMS-TOPUP-{topup.id}',
                            }
                        )
                except Exception as e:
                    logger.warning(f"TumaCallbackMap registration failed for SMS topup: {e}")
                
                return Response({
                    'topup_id': topup.id,
                    'units': units,
                    'amount': str(total_amount),
                    'price_per_unit': str(price_per_unit),
                    'checkout_request_id': topup.checkout_request_id,
                    'message': 'STK push sent. Enter your M-Pesa PIN to complete.',
                }, status=status.HTTP_202_ACCEPTED)
            else:
                topup.status = 'failed'
                topup.notes = res.get('message', 'STK failed')
                topup.save()
                return Response({'error': topup.notes}, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
            logger.error(f"SMS topup STK push failed: {e}")
            topup.status = 'failed'
            topup.notes = str(e)
            topup.save()
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SMSTopupCallbackView(APIView):
    """
    POST /api/v1/messaging/topup/callback/
    Called by Tuma when the SMS top-up payment completes.
    PUBLIC — no auth.
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        data = request.data
        checkout_id = data.get('checkout_request_id', '')
        result_code = str(data.get('result_code', ''))

        # FIX 2: Add logging for missing checkout_id
        if not checkout_id:
            logger.warning("SMSTopupCallbackView: missing checkout_request_id in payload")
            return Response({'ok': True})

        logger.info(
            f"SMSTopupCallbackView received: checkout_id={checkout_id}, "
            f"result_code={result_code}"
        )

        # FIX 6: Resolve tenant schema from the public callback map
        target_schema = None
        try:
            from apps.core.models import TumaCallbackMap
            from django_tenants.utils import schema_context, get_public_schema_name
            with schema_context(get_public_schema_name()):
                mapping = TumaCallbackMap.objects.filter(
                    checkout_request_id=checkout_id,
                    payment_reference__startswith='SMS-TOPUP-'
                ).first()
                if mapping:
                    target_schema = mapping.schema_name
        except Exception as e:
            # FIX 2: Use error level logging with full traceback for TumaCallbackMap failures
            logger.error(
                f"SMSTopup callback: TumaCallbackMap lookup failed for "
                f"checkout_id={checkout_id}: {e}",
                exc_info=True,
            )

        # Fallback: scan tenant schemas (for topups before this fix was deployed)
        if not target_schema:
            try:
                from apps.core.models import Tenant
                from django_tenants.utils import schema_context, get_public_schema_name
                with schema_context(get_public_schema_name()):
                    schemas = list(Tenant.objects.filter(
                        is_active=True
                    ).exclude(schema_name='public').values_list('schema_name', flat=True))
                for s in schemas:
                    with schema_context(s):
                        exists = SMSUnitTopup.objects.filter(
                            checkout_request_id=checkout_id
                        ).exists()
                        if exists:
                            target_schema = s
                            break
            except Exception as e:
                logger.error(f"SMSTopup schema scan failed: {e}")

        # ============================================================
        # FIX: Validate target_schema is not public before switching
        # ============================================================
        if not target_schema or target_schema in ('public', ''):
            logger.warning(f"SMSTopup callback: target_schema is '{target_schema}', rejecting safely")
            return Response({'ok': True, 'warning': 'invalid_target_schema'})

        # Process the topup in the correct tenant schema
        from django_tenants.utils import schema_context
        from django.db.utils import ProgrammingError
        
        with schema_context(target_schema):
            try:
                topup = SMSUnitTopup.objects.filter(checkout_request_id=checkout_id).first()
            except ProgrammingError:
                logger.error(f"messaging_smsunittopup table missing in schema={target_schema}")
                return Response({'ok': True, 'warning': 'schema_not_migrated'})

            if not topup or topup.status == 'completed':
                return Response({'ok': True})  # idempotent

            if result_code == '0':
                topup.status = 'completed'
                topup.save()

                # Credit wallet in the correct tenant schema
                from django.db import transaction as _tx
                with _tx.atomic():
                    wallet, _ = TenantSMSWallet.objects.get_or_create(
                        is_active=True,
                        defaults={
                            'sms_units': Decimal('0'),
                            'sell_price_per_unit': Decimal('0.40'),
                        },
                    )
                    w = TenantSMSWallet.objects.select_for_update().get(pk=wallet.pk)
                    w.sms_units += Decimal(str(topup.units_purchased))
                    w.save(update_fields=['sms_units', 'updated_at'])

                from .models import SMSCreditLedger
                SMSCreditLedger.objects.create(
                    wallet=wallet,
                    entry_type='topup',
                    units=Decimal(str(topup.units_purchased)),
                    unit_price=wallet.sell_price_per_unit,
                    amount=topup.amount_paid,
                    reference=topup.payment_reference,
                    notes=f'Top-up #{topup.id} ({topup.units_purchased} units)',
                )
                logger.info(f"Credited {topup.units_purchased} SMS units to {target_schema}")
            else:
                topup.status = 'failed'
                topup.notes = data.get('result_desc', 'Payment failed')
                topup.save()

        return Response({'ok': True})


# ─────────────────────────────────────────────────────────────────────────────
# FIX 4: Customer Search Endpoint
# ─────────────────────────────────────────────────────────────────────────────

class CustomerSearchView(APIView):
    """
    GET /api/v1/messaging/customers/search/?q=john&type=pppoe&limit=20
    
    Search customers for the SMS compose dialog.
    type: pppoe | hotspot | all
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        q = request.query_params.get('q', '').strip()
        ctype = request.query_params.get('type', 'all')
        limit = min(int(request.query_params.get('limit', 20)), 100)

        results = []

        if ctype in ('pppoe', 'all'):
            from apps.customers.models import Customer
            from django.db.models import Q
            qs = Customer.objects.filter(status='ACTIVE').select_related('user')
            if q:
                qs = qs.filter(
                    Q(user__first_name__icontains=q)
                    | Q(user__last_name__icontains=q)
                    | Q(user__phone_number__icontains=q)
                    | Q(customer_code__icontains=q)
                )
            for c in qs[:limit]:
                phone = getattr(c.user, 'phone_number', '') or ''
                if phone:
                    results.append({
                        'id': str(c.id),
                        'name': c.full_name,
                        'phone': phone,
                        'code': c.customer_code,
                        'type': 'pppoe',
                    })

        if ctype in ('hotspot', 'all'):
            from apps.billing.models.hotspot_models import HotspotClient
            from django.db.models import Q
            qs = HotspotClient.objects.filter(
                canonical_phone__isnull=False
            ).exclude(canonical_phone__startswith='MAC-')
            if q:
                qs = qs.filter(
                    Q(canonical_phone__icontains=q)
                    | Q(canonical_username__icontains=q)
                    | Q(email__icontains=q)
                )
            for hc in qs[:limit]:
                results.append({
                    'id': str(hc.id),
                    'name': hc.canonical_username or hc.canonical_phone,
                    'phone': hc.canonical_phone,
                    'code': hc.canonical_username,
                    'type': 'hotspot',
                })

        # Deduplicate by phone
        seen = set()
        unique = []
        for r in results:
            if r['phone'] not in seen:
                seen.add(r['phone'])
                unique.append(r)

        return Response({'results': unique[:limit], 'count': len(unique)})