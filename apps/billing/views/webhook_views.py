"""
PayHero Webhook Handlers & M-Pesa C2B Webhook

PUBLIC ENDPOINTS - These receive callbacks from PayHero and Safaricom when payments complete.

Webhooks:
1. /api/v1/webhooks/payhero/subscription/ - ISP subscription payments
2. /api/v1/webhooks/payhero/hotspot/ - Hotspot WiFi purchases
3. /api/v1/webhooks/payhero/billing/ - Customer invoice/recharge payments
4. /api/v1/webhooks/mpesa/c2b-callback/ - Direct M-Pesa Paybill payments (Money -> Active Internet)
"""

import json
import logging
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from django_tenants.utils import schema_context, get_public_schema_name

from apps.billing.services.payhero import PayHeroClient
from apps.customers.models import ServiceConnection
from apps.billing.models.payment_models import MpesaConfiguration, Payment, MpesaTransaction, InvoiceItemPayment

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
                payment.mpesa_receipt = payload['mpesa_receipt']
                payment.processed_at = timezone.now()
                payment.save()
                
                # Apply to customer balance or invoice
                customer = payment.customer
                if payment.invoice:
                    # Apply to invoice
                    payment.invoice.add_payment(payment.amount, payment.payment_method)
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


class MpesaC2BWebhookView(APIView):
    """
    Receives Customer-to-Business (C2B) Paybill payments from Safaricom.
    This is where the automation happens (Money -> Active Internet).
    
    POST /api/v1/webhooks/mpesa/c2b-callback/
    
    Expected Payload:
    {
        "TransactionType": "Pay Bill",
        "TransID": "RKTQDM7W6S",
        "TransTime": "20191114121845",
        "TransAmount": "1000.00",
        "BusinessShortCode": "123456",
        "BillRefNumber": "CUST12345",
        "InvoiceNumber": "",
        "OrgAccountBalance": "50000.00",
        "ThirdPartyTransID": "",
        "MSISDN": "254712345678",
        "FirstName": "John",
        "MiddleName": "Doe",
        "LastName": "Smith"
    }
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    
    @transaction.atomic
    def post(self, request, *args, **kwargs):
        data = request.data
        logger.info(f"M-Pesa Paybill Callback: {json.dumps(data)}")
        
        # Extract key fields from Safaricom callback
        bill_ref = data.get('BillRefNumber', '').strip().upper()
        trans_id = data.get('TransID')
        amount = Decimal(str(data.get('TransAmount', 0)))
        msisdn = data.get('MSISDN')
        trans_time = data.get('TransTime')
        business_shortcode = data.get('BusinessShortCode')
        
        # Validate required fields
        if not trans_id:
            logger.error("M-Pesa callback missing TransID")
            return Response(
                {"ResultCode": 1, "ResultDesc": "Missing Transaction ID"},
                status=status.HTTP_200_OK  # Always return 200 to Safaricom
            )
        
        if not bill_ref:
            logger.warning(f"Payment {trans_id} has no BillRefNumber")
            # Return success to Safaricom but flag for manual reconciliation
            return Response(
                {"ResultCode": 0, "ResultDesc": "Success - No Account Reference"},
                status=status.HTTP_200_OK
            )
        
        # Find the tenant from the business shortcode
        # This tells us which ISP this payment belongs to
        config = MpesaConfiguration.objects.filter(
            business_shortcode=business_shortcode,
            is_active=True
        ).select_related().first()
        
        if not config:
            logger.error(f"No active M-Pesa configuration found for shortcode: {business_shortcode}")
            return Response(
                {"ResultCode": 1, "ResultDesc": "Invalid Business Shortcode"},
                status=status.HTTP_200_OK
            )
        
        # Process in the correct tenant schema
        with schema_context(config.schema_name):
            # 1. Find the customer across our accounting fields
            service = ServiceConnection.objects.filter(
                models.Q(billing_account_number__iexact=bill_ref) |
                models.Q(mpesa_account_number__iexact=bill_ref) |
                models.Q(paybill_account_number__iexact=bill_ref)
            ).select_related('customer').first()
            
            if not service:
                logger.warning(
                    f"Payment {trans_id} for unknown account {bill_ref} "
                    f"(Tenant: {config.schema_name}). Requires manual audit."
                )
                # Create a transaction record for manual reconciliation
                MpesaTransaction.objects.create(
                    configuration=config,
                    schema_name=config.schema_name,
                    merchant_request_id=f"MANUAL-{trans_id}",
                    checkout_request_id=f"MANUAL-{trans_id}",
                    transaction_id=trans_id,
                    amount=amount,
                    phone_number=msisdn,
                    account_reference=bill_ref,
                    status='PENDING',
                    result_code=999,
                    result_desc="Unknown account - manual reconciliation required",
                    callback_data=data
                )
                return Response(
                    {"ResultCode": 0, "ResultDesc": "Account Not Found - Flagged"},
                    status=status.HTTP_200_OK
                )
            
            # 2. Get or create payment method for M-Pesa Paybill
            payment_method, _ = InvoiceItemPayment.objects.get_or_create(
                method_type='MPESA_PAYBILL',
                defaults={
                    'name': 'M-Pesa Paybill',
                    'code': 'MPESA_PAYBILL',
                    'is_active': True,
                    'schema_name': config.schema_name
                }
            )
            
            # 3. Check if this transaction was already processed (prevent duplicates)
            existing_txn = MpesaTransaction.objects.filter(
                transaction_id=trans_id
            ).first()
            
            if existing_txn:
                logger.info(f"Duplicate M-Pesa callback received for transaction: {trans_id}")
                return Response(
                    {"ResultCode": 0, "ResultDesc": "Success - Already Processed"},
                    status=status.HTTP_200_OK
                )
            
            # 4. Record the transaction
            mpesa_txn = MpesaTransaction.objects.create(
                configuration=config,
                schema_name=config.schema_name,
                merchant_request_id=f"C2B-{trans_id}",
                checkout_request_id=f"C2B-{trans_id}",
                transaction_id=trans_id,
                transaction_type='C2B',
                amount=amount,
                phone_number=msisdn,
                account_reference=bill_ref,
                status='COMPLETED',
                result_code=0,
                result_desc="Success",
                callback_data=data,
                callback_received_at=timezone.now()
            )
            
            # 5. Create the payment record
            payment = Payment.objects.create(
                customer=service.customer,
                amount=amount,
                payment_method=payment_method,
                status='COMPLETED',
                transaction_id=trans_id,
                mpesa_receipt=trans_id,
                mpesa_phone=msisdn,
                payer_phone=msisdn,
                payment_date=timezone.now(),
                schema_name=config.schema_name,
                mpesa_transaction=mpesa_txn
            )
            
            # 6. Link the M-Pesa transaction to the payment
            mpesa_txn.payment = payment
            mpesa_txn.save()
            
            # 7. Apply payment to customer's account
            customer = service.customer
            
            # Check if there's an outstanding invoice for this amount
            from apps.billing.models.billing_models import Invoice
            
            # Look for pending invoices for this customer
            pending_invoice = Invoice.objects.filter(
                customer=customer,
                status__in=['ISSUED', 'OVERDUE'],
                balance__lte=amount + 1  # Allow small rounding differences
            ).order_by('due_date').first()
            
            if pending_invoice:
                # Apply to the oldest pending invoice
                payment.invoice = pending_invoice
                payment.save()
                pending_invoice.add_payment(amount, payment_method)
                logger.info(f"Payment {trans_id} applied to invoice {pending_invoice.invoice_number}")
            else:
                # Add to customer balance for future invoices
                customer.balance = (customer.balance or 0) + amount
                customer.save(update_fields=['balance'])
                logger.info(f"Payment {trans_id} added to customer balance")
            
            # 8. UNLOCK THE INTERNET (MikroTik Integration)
            # Check if customer was suspended and should be reactivated
            if service.status == 'suspended':
                # Check if payment is enough to cover at least one month
                if amount >= service.monthly_price:
                    service.activate_service()  # Sets status to ACTIVE in DB
                    
                    # TODO: Call MikroTik API to remove user from Walled Garden/Expired list
                    # from apps.network.integrations.mikrotik_api import MikrotikAPI
                    # router_api = MikrotikAPI(service.router)
                    # router_api.activate_customer(service)
                    
                    logger.info(
                        f"Service reactivated for {customer.full_name} "
                        f"(Account: {bill_ref}) - Payment: {trans_id}"
                    )
                else:
                    logger.info(
                        f"Payment amount {amount} less than monthly price {service.monthly_price}. "
                        f"Service remains suspended for {customer.full_name}"
                    )
            
            # 9. Send confirmation SMS (optional)
            # TODO: Implement SMS notification
            # from apps.notifications.services import send_sms
            # send_sms(
            #     phone=msisdn,
            #     message=f"Thank you! Payment of KES {amount} received. "
            #             f"Your internet has been reactivated. Receipt: {trans_id}"
            # )
            
            logger.info(
                f"M-Pesa payment processed successfully: {trans_id} - "
                f"Customer: {customer.full_name} - Amount: KES {amount}"
            )
        
        # Always return success to Safaricom with ResultCode 0
        return Response(
            {"ResultCode": 0, "ResultDesc": "Success"},
            status=status.HTTP_200_OK
        )


# URL patterns for webhooks (to be added to main urls.py)
# path('api/v1/webhooks/payhero/subscription/', PayHeroSubscriptionWebhookView.as_view()),
# path('api/v1/webhooks/payhero/hotspot/', PayHeroHotspotWebhookView.as_view()),
# path('api/v1/webhooks/payhero/billing/', PayHeroBillingWebhookView.as_view()),
# path('api/v1/webhooks/mpesa/c2b-callback/', MpesaC2BWebhookView.as_view()),