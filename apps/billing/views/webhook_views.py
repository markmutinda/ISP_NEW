"""
PayHero Webhook Handlers

PUBLIC ENDPOINTS - These receive callbacks from PayHero when payments complete.

Three separate webhooks for different payment types:
1. /api/v1/webhooks/payhero/subscription/ - ISP subscription payments
2. /api/v1/webhooks/payhero/hotspot/ - Hotspot WiFi purchases
3. /api/v1/webhooks/payhero/billing/ - Customer invoice/recharge payments
"""

import json
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from django_tenants.utils import schema_context, get_public_schema_name

from apps.billing.services.payhero import PayHeroClient

logger = logging.getLogger(__name__)


class PayHeroWebhookMixin:
    """Common functionality for PayHero webhooks"""
    
    permission_classes = [AllowAny]
    authentication_classes = []  # PUBLIC - no auth
    
    def verify_signature(self, request) -> bool:
        """Verify PayHero webhook signature"""
        signature = request.headers.get('X-PayHero-Signature', '')
        
        if not signature and not settings.DEBUG:
            logger.warning("PayHero webhook received without signature")
            return False
        
        client = PayHeroClient()
        
        # Get raw body
        try:
            body = request.body
        except Exception:
            body = json.dumps(request.data)
        
        return client.verify_webhook_signature(body, signature)
    
    def parse_payload(self, request) -> dict:
        """Parse and normalize PayHero webhook payload"""
        data = request.data
        
        # PayHero sends different field names in different scenarios
        return {
            'checkout_request_id': (
                data.get('CheckoutRequestID') or 
                data.get('checkout_request_id') or
                data.get('reference')
            ),
            'result_code': data.get('ResultCode', data.get('result_code', 0)),
            'result_desc': data.get('ResultDesc', data.get('result_desc', '')),
            'amount': data.get('Amount', data.get('amount')),
            'mpesa_receipt': (
                data.get('MpesaReceiptNumber') or
                data.get('mpesa_receipt') or
                data.get('provider_reference')
            ),
            'phone_number': data.get('PhoneNumber', data.get('phone_number')),
            'transaction_date': data.get('TransactionDate', data.get('completed_at')),
            'raw': data,
        }


class PayHeroSubscriptionWebhookView(PayHeroWebhookMixin, APIView):
    """
    Webhook for ISP subscription payments (ISP → Netily).
    
    POST /api/v1/webhooks/payhero/subscription/
    """
    
    @transaction.atomic
    def post(self, request):
        logger.info("Received subscription payment webhook")
        
        # Verify signature in production
        if not settings.DEBUG and not self.verify_signature(request):
            logger.warning("Invalid webhook signature for subscription")
            return Response({'error': 'Invalid signature'}, status=status.HTTP_401_UNAUTHORIZED)
        
        payload = self.parse_payload(request)
        checkout_id = payload['checkout_request_id']
        
        if not checkout_id:
            logger.error("Subscription webhook missing checkout_request_id")
            return Response({'error': 'Missing checkout ID'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Find payment in public schema (subscriptions are public)
        with schema_context(get_public_schema_name()):
            from apps.subscriptions.models import SubscriptionPayment
            
            try:
                payment = SubscriptionPayment.objects.select_related(
                    'subscription__company'
                ).get(payhero_checkout_id=checkout_id)
            except SubscriptionPayment.DoesNotExist:
                logger.error(f"Subscription payment not found: {checkout_id}")
                return Response({'error': 'Payment not found'}, status=status.HTTP_404_NOT_FOUND)
            
            result_code = int(payload['result_code'])
            
            if result_code == 0:
                # Success
                payment.mark_completed(payload['mpesa_receipt'])
                
                # Check if this is a trial conversion
                subscription = payment.subscription
                if subscription.is_trial:
                    # Convert from trial to paid subscription
                    subscription.convert_from_trial(billing_period=subscription.billing_period)
                    logger.info(
                        f"Trial converted to paid: {subscription.company.name} "
                        f"- Plan: {subscription.plan.name}"
                    )
                else:
                    # Regular subscription renewal/extension
                    subscription.extend_subscription()
                
                logger.info(
                    f"Subscription payment completed: {subscription.company.name} "
                    f"- KES {payment.amount} - {payload['mpesa_receipt']}"
                )
                
                # TODO: Send confirmation email/SMS
                
            else:
                # Failed
                payment.mark_failed(payload['result_desc'])
                
                logger.info(
                    f"Subscription payment failed: {payment.subscription.company.name} "
                    f"- {payload['result_desc']}"
                )
        
        return Response({'status': 'received'})


class PayHeroHotspotWebhookView(PayHeroWebhookMixin, APIView):
    """
    Webhook for hotspot WiFi purchases (End User → Netily → ISP).
    
    POST /api/v1/webhooks/payhero/hotspot/
    """
    
    @transaction.atomic
    def post(self, request):
        logger.info("Received hotspot payment webhook")
        
        # Verify signature in production
        if not settings.DEBUG and not self.verify_signature(request):
            logger.warning("Invalid webhook signature for hotspot")
            return Response({'error': 'Invalid signature'}, status=status.HTTP_401_UNAUTHORIZED)
        
        payload = self.parse_payload(request)
        checkout_id = payload['checkout_request_id']
        
        if not checkout_id:
            logger.error("Hotspot webhook missing checkout_request_id")
            return Response({'error': 'Missing checkout ID'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Hotspot sessions are in tenant schema
        # We need to find which tenant has this session
        from apps.billing.models.hotspot_models import HotspotSession
        from apps.core.models import Tenant
        
        session = None
        tenant = None
        
        # Search across all tenants
        # In production, you might want to encode tenant info in the reference
        with schema_context(get_public_schema_name()):
            tenants = Tenant.objects.all()
        
        for t in tenants:
            if t.schema_name == get_public_schema_name():
                continue
            
            with schema_context(t.schema_name):
                try:
                    session = HotspotSession.objects.select_related(
                        'router', 'plan'
                    ).get(payhero_checkout_id=checkout_id)
                    tenant = t
                    break
                except HotspotSession.DoesNotExist:
                    continue
        
        if not session:
            logger.error(f"Hotspot session not found: {checkout_id}")
            return Response({'error': 'Session not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Process in the correct tenant schema
        with schema_context(tenant.schema_name):
            result_code = int(payload['result_code'])
            
            if result_code == 0:
                # Success - mark paid and activate
                session.mark_paid(payload['mpesa_receipt'])
                
                # Activate on MikroTik (this would call the router API)
                access_code = self._activate_hotspot_user(session)
                session.activate(access_code)
                
                logger.info(
                    f"Hotspot payment completed: {session.session_id} "
                    f"- KES {session.amount} - {payload['mpesa_receipt']}"
                )
                
                # Record commission in public schema
                with schema_context(get_public_schema_name()):
                    from apps.subscriptions.models import CommissionLedger
                    from apps.core.models import Company
                    
                    try:
                        # Get company from tenant
                        company = Company.objects.filter(
                            tenant__schema_name=tenant.schema_name
                        ).first()
                        
                        if company:
                            CommissionLedger.record_commission(
                                company=company,
                                payment_type='hotspot',
                                payment_reference=session.session_id,
                                gross_amount=session.amount,
                            )
                    except Exception as e:
                        logger.error(f"Error recording hotspot commission: {e}")
            else:
                # Failed
                session.mark_failed(payload['result_desc'])
                
                logger.info(
                    f"Hotspot payment failed: {session.session_id} "
                    f"- {payload['result_desc']}"
                )
        
        return Response({'status': 'received'})
    
    def _activate_hotspot_user(self, session) -> str:
        """
        Activate user on MikroTik router.
        Returns the access code.
        """
        # Generate access code
        access_code = session.generate_access_code()
        
        # TODO: Implement MikroTik API call
        # This would connect to the router and create a hotspot user
        #
        # Example (using librouteros):
        # from librouteros import connect
        # 
        # router = session.router
        # api = connect(
        #     host=router.ip_address,
        #     username=router.api_username,
        #     password=router.api_password,
        #     port=router.api_port,
        # )
        # 
        # api.path('ip', 'hotspot', 'user').add(
        #     name=access_code,
        #     password=access_code,
        #     profile=session.plan.mikrotik_profile,
        #     limit_uptime=f"{session.plan.duration_minutes}m",
        #     mac_address=session.mac_address,
        # )
        
        logger.info(f"Activated hotspot user: {access_code} on {session.router.name}")
        
        return access_code


class PayHeroBillingWebhookView(PayHeroWebhookMixin, APIView):
    """
    Webhook for customer billing payments (Customer → Netily → ISP).
    
    POST /api/v1/webhooks/payhero/billing/
    """
    
    @transaction.atomic
    def post(self, request):
        logger.info("Received billing payment webhook")
        
        # Verify signature in production
        if not settings.DEBUG and not self.verify_signature(request):
            logger.warning("Invalid webhook signature for billing")
            return Response({'error': 'Invalid signature'}, status=status.HTTP_401_UNAUTHORIZED)
        
        payload = self.parse_payload(request)
        checkout_id = payload['checkout_request_id']
        
        if not checkout_id:
            logger.error("Billing webhook missing checkout_request_id")
            return Response({'error': 'Missing checkout ID'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Customer payments are in tenant schema
        from apps.billing.models.payment_models import Payment
        from apps.core.models import Tenant
        
        payment = None
        tenant = None
        
        # Search across all tenants
        with schema_context(get_public_schema_name()):
            tenants = Tenant.objects.all()
        
        for t in tenants:
            if t.schema_name == get_public_schema_name():
                continue
            
            with schema_context(t.schema_name):
                try:
                    payment = Payment.objects.select_related('customer').get(
                        payhero_checkout_id=checkout_id
                    )
                    tenant = t
                    break
                except Payment.DoesNotExist:
                    continue
        
        if not payment:
            logger.error(f"Billing payment not found: {checkout_id}")
            return Response({'error': 'Payment not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Process in the correct tenant schema
        with schema_context(tenant.schema_name):
            result_code = int(payload['result_code'])
            
            if result_code == 0:
                # Success
                payment.status = 'COMPLETED'
                payment.mpesa_receipt_number = payload['mpesa_receipt']
                payment.paid_at = timezone.now()
                payment.save()
                
                # Apply to customer balance or invoice
                customer = payment.customer
                if payment.invoice:
                    # Apply to invoice
                    payment.invoice.apply_payment(payment)
                else:
                    # Add to customer balance
                    customer.balance = (customer.balance or 0) + payment.amount
                    customer.save(update_fields=['balance'])
                
                # Check if customer was suspended and should be reactivated
                if customer.status == 'suspended' and customer.balance >= 0:
                    customer.status = 'active'
                    customer.save(update_fields=['status'])
                    # TODO: Reactivate on router
                
                logger.info(
                    f"Billing payment completed: {customer.full_name} "
                    f"- KES {payment.amount} - {payload['mpesa_receipt']}"
                )
                
                # Record commission in public schema
                with schema_context(get_public_schema_name()):
                    from apps.subscriptions.models import CommissionLedger
                    from apps.core.models import Company
                    
                    try:
                        company = Company.objects.filter(
                            tenant__schema_name=tenant.schema_name
                        ).first()
                        
                        if company:
                            CommissionLedger.record_commission(
                                company=company,
                                payment_type='invoice' if payment.invoice else 'recharge',
                                payment_reference=str(payment.id),
                                gross_amount=payment.amount,
                            )
                    except Exception as e:
                        logger.error(f"Error recording billing commission: {e}")
                
                # TODO: Send confirmation SMS
            else:
                # Failed
                payment.status = 'FAILED'
                payment.failure_reason = payload['result_desc']
                payment.save()
                
                logger.info(
                    f"Billing payment failed: {payment.customer.full_name} "
                    f"- {payload['result_desc']}"
                )
        
        return Response({'status': 'received'})


# URL patterns for webhooks (to be added to main urls.py)
# path('api/v1/webhooks/payhero/subscription/', PayHeroSubscriptionWebhookView.as_view()),
# path('api/v1/webhooks/payhero/hotspot/', PayHeroHotspotWebhookView.as_view()),
# path('api/v1/webhooks/payhero/billing/', PayHeroBillingWebhookView.as_view()),
