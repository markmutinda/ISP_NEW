# apps/messaging/views.py
from decimal import Decimal
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.views import APIView
from django.db import transaction
from django.db.models import Count, Sum, Q
from django.utils import timezone
from datetime import timedelta
from rest_framework import serializers
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
from .services.credit_billing_service import CreditBillingService


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
    permission_classes = [IsAuthenticated, IsAdminUser]

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
        return Response(serializer.data)


class SMSWalletView(APIView):
    """
    GET /api/v1/messaging/wallet/
    Returns current balance + last 20 topup records.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

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

    Calculates cost, creates a pending topup record, initiates STK push.
    """
    permission_classes = [IsAuthenticated, IsAdminUser]

    # FIX 3: Flat pricing at KES 0.40 per unit
    UNIT_PRICE = _Decimal('0.40')

    def _price_for(self, units: int) -> _Decimal:
        return self.UNIT_PRICE

    def post(self, request):
        units = int(request.data.get('units', 0))
        phone = request.data.get('phone_number', '')

        # FIX 4: Minimum top-up is 25 units (KES 10)
        if units < 25:
            return Response(
                {'error': 'Minimum top-up is 25 units (KES 10)'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not phone:
            return Response(
                {'error': 'phone_number is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        price_per_unit = self._price_for(units)
        total_amount = (price_per_unit * units).quantize(_Decimal('0.01'))

        topup = SMSUnitTopup.objects.create(
            units_purchased=units,
            amount_paid=total_amount,
            payment_method='mpesa_stk',
            status='pending',
        )

        # Kick off STK push via Tuma (reuse billing payment flow)
        try:
            from django.conf import settings as _settings
            from django.db import connection
            from apps.billing.models.payment_models import TenantTumaConfig, InvoiceItemPayment, Payment
            from apps.billing.services.tuma_service import TumaClient
            import time

            schema = connection.schema_name
            cfg = TenantTumaConfig.objects.filter(schema_name=schema, is_active=True).first()
            if not cfg or not cfg.tuma_business_email:
                topup.status = 'failed'
                topup.notes = 'Tuma not configured'
                topup.save()
                return Response(
                    {'error': 'Payment gateway not configured'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            client = TumaClient()
            token = client.get_token(cfg.tuma_business_email, cfg.tuma_business_api_key)
            callback_url = getattr(_settings, 'TUMA_CALLBACK_URL', '')
            desc = f"SMS-UNITS-{topup.id}"

            res = client.stk_push(
                token=token,
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
                return Response({
                    'topup_id': topup.id,
                    'units': units,
                    'amount': str(total_amount),
                    'checkout_request_id': topup.checkout_request_id,
                    'message': 'STK push sent. Enter your M-Pesa PIN to complete.',
                }, status=status.HTTP_202_ACCEPTED)
            else:
                topup.status = 'failed'
                topup.notes = res.get('message', 'STK failed')
                topup.save()
                return Response({'error': topup.notes}, status=status.HTTP_400_BAD_REQUEST)

        except Exception as e:
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

        topup = SMSUnitTopup.objects.filter(checkout_request_id=checkout_id).first()
        if not topup:
            return Response({'ok': True})

        if result_code == '0':
            topup.status = 'completed'
            topup.save()

            # Credit the wallet
            wallet, _ = TenantSMSWallet.objects.get_or_create(
                pk=1,
                defaults={'sms_units': _Decimal('0'), 'sell_price_per_unit': _Decimal('0.60')},
            )
            from django.db import transaction as _tx
            with _tx.atomic():
                w = TenantSMSWallet.objects.select_for_update().get(pk=wallet.pk)
                w.sms_units += _Decimal(str(topup.units_purchased))
                w.save(update_fields=['sms_units', 'updated_at'])

            from .models import SMSCreditLedger
            SMSCreditLedger.objects.create(
                wallet=wallet,
                entry_type='topup',
                units=_Decimal(str(topup.units_purchased)),
                unit_price=wallet.sell_price_per_unit,
                amount=topup.amount_paid,
                reference=topup.payment_reference,
                notes=f'Top-up #{topup.id}',
            )
        else:
            topup.status = 'failed'
            topup.notes = data.get('result_desc', 'Payment failed')
            topup.save()

        return Response({'ok': True})