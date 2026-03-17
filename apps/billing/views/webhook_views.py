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
from django.db import models, transaction, IntegrityError, connection
from django.utils import timezone
from django.db.models import Q

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from django_tenants.utils import schema_context, get_public_schema_name

from apps.billing.services.payhero import PayHeroClient
from apps.customers.models import ServiceConnection
from apps.billing.models.payment_models import MpesaConfiguration, Payment, MpesaTransaction, InvoiceItemPayment
from apps.network.integrations.mikrotik_api import MikrotikAPI
from apps.core.models import Tenant

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
                    # FIXED: Use 'outstanding_balance' instead of 'balance'
                    customer.outstanding_balance = (customer.outstanding_balance or 0) + payment.amount
                    customer.save(update_fields=['outstanding_balance'])
                
                # Check if customer was suspended and should be reactivated
                if customer.status == 'SUSPENDED' and customer.outstanding_balance >= 0:
                    customer.status = 'ACTIVE'
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
    Production-Grade M-Pesa C2B Webhook.
    Handles RADIUS Expiry Extension, Unique ID Generation, and MikroTik Session Kick.
    
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
    
    def trigger_mikrotik_reactivation(self, service):
        """
        Connects to the MikroTik router and clears the active PPPoE session.
        The router is found via the CustomerRadiusCredentials linked to the service.
        """
        try:
            from apps.network.integrations.mikrotik_api import MikrotikAPI
            
            # 1. Identify which RADIUS credential and router to use
            # We check PPPoE first, then fallback to Hotspot
            radius_cred = service.pppoe_user or service.hotspot_user
            
            if not radius_cred:
                logger.warning(f"Skipping MikroTik kick: No RADIUS credentials found for service {service.id}")
                return
                
            if not radius_cred.router:
                logger.warning(f"Skipping MikroTik kick: No router assigned to RADIUS credentials for service {service.id}")
                return

            # 2. Connect to the router using the credential's router relation
            api = MikrotikAPI(radius_cred.router)
            
            # 3. Kick the user by their RADIUS username
            success = api.kick_pppoe_user(radius_cred.username)
            
            if success:
                logger.info(f"MikroTik: Successfully kicked session for {radius_cred.username} on {radius_cred.router.name}")
            else:
                logger.info(f"MikroTik: No active session for {radius_cred.username} (User already offline)")

        except Exception as e:
            logger.error(f"Failed MikroTik kick for service {service.id}: {str(e)}", exc_info=True)
    
    def post(self, request, *args, **kwargs):
        data = request.data
        trans_id = data.get('TransID')
        shortcode = data.get('BusinessShortCode')
        bill_ref = data.get('BillRefNumber', '').strip().upper()
        amount = Decimal(str(data.get('TransAmount', 0)))
        msisdn = data.get('MSISDN', '')

        # Mask PII in logs
        safe_phone = f"****{msisdn[-4:]}" if msisdn and len(msisdn) >= 4 else "Unknown"
        logger.info(f"C2B Webhook: ID={trans_id} | Ref={bill_ref} | SC={shortcode} | Phone={safe_phone}")

        # Validate required fields
        if not trans_id:
            logger.error("M-Pesa callback missing TransID")
            return Response(
                {"ResultCode": 1, "ResultDesc": "Missing ID"},
                status=status.HTTP_200_OK  # Always return 200 to Safaricom
            )
        
        if not bill_ref:
            logger.warning(f"Payment {trans_id} has no BillRefNumber")
            # Return success to Safaricom but flag for manual reconciliation
            return Response(
                {"ResultCode": 0, "ResultDesc": "Success - No Account Reference"},
                status=status.HTTP_200_OK
            )

        # 1. FIND THE TENANT
        target_tenant_schema = None
        if connection.schema_name == 'public':
            for tenant in Tenant.objects.exclude(schema_name='public'):
                with schema_context(tenant.schema_name):
                    try:
                        # Find if this ISP owns the shortcode and the customer
                        if MpesaConfiguration.objects.filter(business_shortcode=shortcode, is_active=True).exists():
                            if ServiceConnection.objects.filter(
                                models.Q(billing_account_number__iexact=bill_ref) |
                                models.Q(mpesa_account_number__iexact=bill_ref)
                            ).exists():
                                target_tenant_schema = tenant.schema_name
                                logger.info(f"Found matching tenant: {tenant.schema_name}")
                                break
                    except Exception as e:
                        # Skip if the table doesn't exist in this specific schema yet
                        logger.debug(f"Error checking tenant {tenant.schema_name}: {str(e)}")
                        continue
        else:
            target_tenant_schema = connection.schema_name

        # 2. IF NO TENANT FOUND, LOG ONLY (Don't try to save to DB)
        if not target_tenant_schema:
            logger.warning(
                f"UNMATCHED PAYMENT: ID={trans_id}, Account={bill_ref}, SC={shortcode}. "
                f"No DB record created. Manual reconciliation required."
            )
            return Response(
                {"ResultCode": 0, "ResultDesc": "Account Not Found"},
                status=status.HTTP_200_OK
            )

        # 3. PROCESS INSIDE TENANT SCHEMA
        with schema_context(target_tenant_schema):
            try:
                with transaction.atomic():
                    # Get the service connection
                    # FIXED: Removed 'router' from select_related - router is accessed via RADIUS credentials
                    service = ServiceConnection.objects.filter(
                        models.Q(billing_account_number__iexact=bill_ref) |
                        models.Q(mpesa_account_number__iexact=bill_ref)
                    ).select_related(
                        'customer', 
                        'plan', 
                        'pppoe_user',      # Include PPPoE credentials
                        'hotspot_user'      # Include Hotspot credentials
                    ).first()

                    if not service:
                        logger.warning(f"Service not found for account {bill_ref} in tenant {target_tenant_schema}")
                        return Response(
                            {"ResultCode": 0, "ResultDesc": "Account Missing"},
                            status=status.HTTP_200_OK
                        )

                    # Get the M-Pesa configuration
                    config = MpesaConfiguration.objects.filter(
                        business_shortcode=shortcode, 
                        is_active=True
                    ).first()

                    if not config:
                        logger.error(f"Active M-Pesa configuration not found for shortcode: {shortcode}")
                        return Response(
                            {"ResultCode": 1, "ResultDesc": "Configuration Error"},
                            status=status.HTTP_200_OK
                        )

                    # A. Idempotent Transaction Log (FIXED: Added Unique Merchant/Checkout IDs)
                    try:
                        mpesa_txn = MpesaTransaction.objects.create(
                            configuration=config,
                            transaction_id=trans_id,
                            merchant_request_id=f"C2B-{trans_id}",  # Unique merchant_request_id
                            checkout_request_id=f"C2B-{trans_id}",  # Unique checkout_request_id
                            transaction_type='C2B',
                            amount=amount,
                            phone_number=msisdn,
                            account_reference=bill_ref,
                            status='COMPLETED',
                            callback_data=data,
                            callback_received_at=timezone.now(),
                            schema_name=target_tenant_schema
                        )
                    except IntegrityError:
                        logger.info(f"Duplicate callback ignored: {trans_id}")
                        return Response(
                            {"ResultCode": 0, "ResultDesc": "Duplicate"},
                            status=status.HTTP_200_OK
                        )

                    # B. Find or create payment method
                    method, _ = InvoiceItemPayment.objects.get_or_create(
                        method_type='MPESA_PAYBILL',
                        defaults={
                            'name': 'M-Pesa Paybill',
                            'code': 'MPESA_PAYBILL',
                            'is_active': True,
                            'schema_name': target_tenant_schema
                        }
                    )
                    
                    # C. Record Payment
                    payment = Payment.objects.create(
                        customer=service.customer,
                        amount=amount,
                        payment_method=method,
                        status='COMPLETED',
                        transaction_id=trans_id,
                        mpesa_receipt=trans_id,
                        mpesa_phone=msisdn,
                        payer_phone=msisdn,
                        payment_date=timezone.now(),
                        mpesa_transaction=mpesa_txn,
                        schema_name=target_tenant_schema
                    )

                    # Link the M-Pesa transaction to the payment
                    mpesa_txn.payment = payment
                    mpesa_txn.save()

                    # D. Apply payment to customer's account
                    customer = service.customer
                    
                    # Look for pending invoices for this customer
                    from apps.billing.models.billing_models import Invoice
                    pending_invoice = Invoice.objects.filter(
                        customer=customer,
                        status__in=['ISSUED', 'OVERDUE'],
                        balance__lte=amount + 1  # Allow small rounding differences
                    ).order_by('due_date').first()
                    
                    if pending_invoice:
                        # Apply to the oldest pending invoice
                        payment.invoice = pending_invoice
                        payment.save()
                        pending_invoice.add_payment(amount, method)
                        logger.info(f"Payment {trans_id} applied to invoice {pending_invoice.invoice_number}")
                    else:
                        # Use 'outstanding_balance' and reduce it by the payment amount
                        # Ensure we handle None/Null values
                        if customer.outstanding_balance is None:
                            customer.outstanding_balance = Decimal('0')
                        
                        # Reduce the outstanding balance by the payment amount
                        customer.outstanding_balance -= amount
                        customer.save(update_fields=['outstanding_balance'])
                        logger.info(f"Payment {trans_id} reduced outstanding balance to {customer.outstanding_balance}")

                    # E. RADIUS ACTIVATION & EXPIRY EXTENSION
                    monthly_price = Decimal(str(service.monthly_price)) if service.monthly_price else Decimal('0')
                    
                    if amount >= monthly_price:
                        service.activate_service()
                        
                        if service.plan:
                            # Import the correct model name - CustomerRadiusCredentials
                            from apps.radius.models import CustomerRadiusCredentials
                            new_expiry = service.plan.calculate_expiration()
                            
                            radius_cred = CustomerRadiusCredentials.objects.filter(customer=customer).first()
                            if radius_cred:
                                radius_cred.expiration_date = new_expiry
                                radius_cred.is_enabled = True
                                radius_cred.save()
                                logger.info(f"Extended Expiry for {bill_ref} to {new_expiry}")
                            else:
                                logger.warning(f"No RADIUS credentials found for customer {customer.full_name}")

                        # Check if the customer profile needs status update
                        if customer.status == 'SUSPENDED':
                            customer.status = 'ACTIVE'
                            customer.save(update_fields=['status'])

                        # F. MIKROTIK KICK - Force immediate reconnection with new expiry
                        self.trigger_mikrotik_reactivation(service)
                        
                        logger.info(f"SUCCESS: Service {bill_ref} reactivated, expiry extended, and MikroTik kicked for {customer.full_name}")
                    elif service.status == 'SUSPENDED':
                        logger.info(
                            f"Payment amount {amount} less than monthly price {monthly_price}. "
                            f"Service remains suspended for {customer.full_name}"
                        )
                    
                    # G. Send confirmation SMS (optional)
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

            except Exception as e:
                logger.error(f"Internal Error in {target_tenant_schema}: {str(e)}", exc_info=True)
                return Response(
                    {"ResultCode": 1, "ResultDesc": "Internal Error"},
                    status=status.HTTP_200_OK
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
# path('api/v1/webhooks/mpesa/c2b-callback/', MpesaC2BWebhookView.as_view()),),