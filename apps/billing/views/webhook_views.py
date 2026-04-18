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
    
    🚨 FIXED: Added strict idempotency and prevented double-extension.
    - select_for_update() prevents race conditions
    - Early return for already processed payments
    - Manual status update instead of mark_completed() to avoid double-firing
    - Single lifecycle transition (either convert_from_trial OR extend_subscription)
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
                # ─── P0 FIX: STRICT IDEMPOTENCY ───
                # select_for_update() locks the row so concurrent webhooks wait in line
                payment = SubscriptionPayment.objects.select_for_update().select_related(
                    'subscription__company', 'subscription__plan'
                ).get(payhero_checkout_id=checkout_id)
            except SubscriptionPayment.DoesNotExist:
                logger.error(f"Subscription payment not found: {checkout_id}")
                return Response({'error': 'Payment not found'}, status=status.HTTP_404_NOT_FOUND)
            
            # ─── P0 FIX: PREVENT DOUBLE PROCESSING ───
            if payment.status in ['completed', 'failed']:
                logger.info(f"Payment {checkout_id} already processed. Ignoring duplicate webhook.")
                return Response({'status': 'already_processed'}, status=status.HTTP_200_OK)
            
            result_code = int(payload['result_code'])
            
            if result_code == 0:
                # Success - UPDATE PAYMENT ONLY (do not call mark_completed to avoid double-firing)
                payment.status = 'completed'
                payment.completed_at = timezone.now()
                payment.mpesa_receipt = payload['mpesa_receipt']
                payment.save(update_fields=['status', 'completed_at', 'mpesa_receipt'])
                
                # ─── APPLY INTENDED PLAN (only on successful payment) ───
                payment.apply_intended_plan()
                
                # ─── SINGLE LIFECYCLE TRANSITION ───
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
                
                # Send cycle activation confirmation email (async)
                from apps.subscriptions.tasks import send_cycle_activated_email
                send_cycle_activated_email.delay(subscription.company_id)
                
            else:
                # Failed
                payment.status = 'failed'
                payment.failure_reason = payload['result_desc']
                payment.save(update_fields=['status', 'failure_reason'])
                
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
                    session = HotspotSession.objects.select_for_update().select_related(
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
            # ─── P0 FIX: PREVENT DOUBLE PROCESSING ───
            if session.status in ['paid', 'active']:
                logger.info(f"Hotspot session {checkout_id} already processed. Ignoring duplicate.")
                return Response({'status': 'already_processed'}, status=status.HTTP_200_OK)
            
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
                    payment = Payment.objects.select_for_update().select_related('customer').get(
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
            # ─── P0 FIX: PREVENT DOUBLE PROCESSING ───
            if payment.status in ['COMPLETED', 'FAILED']:
                logger.info(f"Billing payment {checkout_id} already processed. Ignoring duplicate.")
                return Response({'status': 'already_processed'}, status=status.HTTP_200_OK)
            
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


# ═══════════════════════════════════════════════════════════════════
# ENHANCED M-PESA C2B WEBHOOK WITH PLAN-QUANTITY CALCULATION
# ═══════════════════════════════════════════════════════════════════

class MpesaC2BWebhookView(APIView):
    """
    Production-Grade M-Pesa C2B Webhook — now with plan-quantity calculation.

    When a customer pays via Paybill using their billing account number:
    - Exact plan amount → 1 period of the plan
    - 2× plan amount → 2 periods (stacked/queued)
    - Partial amount → recorded but service NOT activated (requires full plan amount)
    
    POST /api/v1/webhooks/mpesa/c2b-callback/
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def _calculate_renewal_expiry(self, service, amount, current_expiry=None):
        """
        Calculate new expiry date based on payment amount vs plan price.
        
        Returns: (quantity, new_expiry_datetime) or (0, None) if insufficient amount.
        """
        from decimal import Decimal
        from django.utils import timezone

        plan = service.plan
        if not plan:
            return 0, None

        plan_price = Decimal(str(plan.base_price or 0))
        if plan_price <= 0:
            return 0, None

        amount = Decimal(str(amount))
        if amount < plan_price:
            logger.info(
                f"Payment {amount} < plan price {plan_price} — "
                f"insufficient for activation of {service.id}"
            )
            return 0, None

        # Calculate how many full plan periods the amount covers
        quantity = int(amount / plan_price)
        if quantity < 1:
            return 0, None

        # Start from: current expiry (if active) or now
        now = timezone.now()
        if current_expiry and current_expiry > now:
            # Stack onto existing subscription
            start = current_expiry
        else:
            start = now

        # Get plan validity as timedelta
        validity_delta = plan.get_validity_timedelta()
        if not validity_delta:
            # Unlimited plan — just activate
            new_expiry = None
        else:
            new_expiry = start + (validity_delta * quantity)

        logger.info(
            f"Plan renewal: amount={amount}, price={plan_price}, "
            f"quantity={quantity}, start={start}, new_expiry={new_expiry}"
        )
        return quantity, new_expiry

    def trigger_mikrotik_reactivation(self, service):
        """Force MikroTik to reconnect the user immediately with updated RADIUS."""
        try:
            from apps.network.integrations.mikrotik_api import MikrotikAPI

            radius_cred = getattr(service, 'pppoe_user', None) or getattr(service, 'hotspot_user', None)
            if not radius_cred:
                logger.warning(f"No RADIUS credentials linked to service {service.id}")
                return
            if not radius_cred.router:
                logger.warning(f"No router for RADIUS user {radius_cred.username}")
                return

            api = MikrotikAPI(radius_cred.router)
            success = api.kick_pppoe_user(radius_cred.username)
            if success:
                logger.info(f"MikroTik kicked {radius_cred.username} on {radius_cred.router.name}")
        except Exception as e:
            logger.error(f"MikroTik kick failed for service {service.id}: {e}", exc_info=True)

    def post(self, request, *args, **kwargs):
        data = request.data
        trans_id = data.get('TransID')
        shortcode = data.get('BusinessShortCode')
        bill_ref = data.get('BillRefNumber', '').strip().upper()
        amount = Decimal(str(data.get('TransAmount', 0)))
        msisdn = data.get('MSISDN', '')

        safe_phone = f"****{msisdn[-4:]}" if msisdn and len(msisdn) >= 4 else "Unknown"
        logger.info(f"C2B Webhook: ID={trans_id} | Ref={bill_ref} | SC={shortcode} | Phone={safe_phone} | Amount={amount}")

        if not trans_id:
            logger.error("M-Pesa callback missing TransID")
            return Response(
                {"ResultCode": 1, "ResultDesc": "Missing ID"},
                status=status.HTTP_200_OK
            )

        if not bill_ref:
            logger.warning(f"Payment {trans_id} has no BillRefNumber")
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
                        if MpesaConfiguration.objects.filter(
                            business_shortcode=shortcode, is_active=True
                        ).exists():
                            if ServiceConnection.objects.filter(
                                models.Q(billing_account_number__iexact=bill_ref) |
                                models.Q(mpesa_account_number__iexact=bill_ref)
                            ).exists():
                                target_tenant_schema = tenant.schema_name
                                logger.info(f"Found matching tenant: {tenant.schema_name}")
                                break
                    except Exception as e:
                        logger.debug(f"Error checking tenant {tenant.schema_name}: {e}")
                        continue
        else:
            target_tenant_schema = connection.schema_name

        if not target_tenant_schema:
            logger.warning(
                f"UNMATCHED PAYMENT: ID={trans_id}, Account={bill_ref}, SC={shortcode}. "
                "No tenant matched. Manual reconciliation required."
            )
            return Response(
                {"ResultCode": 0, "ResultDesc": "Account Not Found"},
                status=status.HTTP_200_OK
            )

        # 2. PROCESS INSIDE TENANT SCHEMA
        with schema_context(target_tenant_schema):
            try:
                with transaction.atomic():
                    # --- Find service ---
                    service = ServiceConnection.objects.filter(
                        models.Q(billing_account_number__iexact=bill_ref) |
                        models.Q(mpesa_account_number__iexact=bill_ref)
                    ).select_related(
                        'customer', 'plan',
                        'pppoe_user', 'hotspot_user'
                    ).first()

                    if not service:
                        logger.warning(f"Service not found for account {bill_ref} in {target_tenant_schema}")
                        return Response(
                            {"ResultCode": 0, "ResultDesc": "Account Missing"},
                            status=status.HTTP_200_OK
                        )

                    config = MpesaConfiguration.objects.filter(
                        business_shortcode=shortcode, is_active=True
                    ).first()

                    if not config:
                        logger.error(f"No active M-Pesa config for shortcode {shortcode}")
                        return Response(
                            {"ResultCode": 1, "ResultDesc": "Configuration Error"},
                            status=status.HTTP_200_OK
                        )

                    # --- Idempotency: deduplicate by TransID ---
                    try:
                        mpesa_txn = MpesaTransaction.objects.create(
                            configuration=config,
                            transaction_id=trans_id,
                            merchant_request_id=f"C2B-{trans_id}",
                            checkout_request_id=f"C2B-{trans_id}",
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
                        logger.info(f"Duplicate C2B callback ignored: {trans_id}")
                        return Response(
                            {"ResultCode": 0, "ResultDesc": "Duplicate"},
                            status=status.HTTP_200_OK
                        )

                    # --- Find or create payment method ---
                    method, _ = InvoiceItemPayment.objects.get_or_create(
                        method_type='MPESA_PAYBILL',
                        schema_name=target_tenant_schema,
                        defaults={
                            'name': 'M-Pesa Paybill',
                            'code': f'MPESA_PAYBILL_{target_tenant_schema[:10]}',
                            'is_active': True,
                        }
                    )

                    # --- Record payment ---
                    payment = Payment.objects.create(
                        customer=service.customer,
                        amount=amount,
                        payment_method=method,
                        status='COMPLETED',
                        transaction_id=trans_id,
                        mpesa_receipt=trans_id,
                        mpesa_phone=msisdn,
                        payer_phone=msisdn,
                        mpesa_transaction=mpesa_txn,
                        payment_date=timezone.now(),
                        schema_name=target_tenant_schema,
                        notes=f"C2B payment via Paybill. Account: {bill_ref}. Ref: {trans_id}"
                    )
                    mpesa_txn.payment = payment
                    mpesa_txn.save(update_fields=['payment'])

                    # --- Apply to outstanding invoices ---
                    from apps.billing.models.billing_models import Invoice
                    pending_invoices = Invoice.objects.filter(
                        customer=service.customer,
                        status__in=['ISSUED', 'OVERDUE', 'PARTIAL'],
                        balance__gt=0
                    ).order_by('due_date')

                    remaining_amount = amount
                    for invoice in pending_invoices:
                        if remaining_amount <= 0:
                            break
                        apply = min(remaining_amount, invoice.balance)
                        invoice.add_payment(apply, method)
                        remaining_amount -= apply
                        logger.info(f"Applied {apply} to invoice {invoice.invoice_number}")

                    # Update customer outstanding balance
                    customer = service.customer
                    if customer.outstanding_balance is None:
                        customer.outstanding_balance = Decimal('0')
                    customer.outstanding_balance = max(
                        Decimal('0'),
                        customer.outstanding_balance - amount
                    )
                    customer.save(update_fields=['outstanding_balance'])

                    # --- PLAN-BASED QUANTITY RENEWAL ---
                    from apps.radius.models import CustomerRadiusCredentials

                    radius_cred = CustomerRadiusCredentials.objects.filter(
                        customer=customer
                    ).first()

                    # Get current expiry from RADIUS credentials
                    current_expiry = radius_cred.expiration_date if radius_cred else None

                    quantity, new_expiry = self._calculate_renewal_expiry(
                        service, amount, current_expiry
                    )

                    if quantity >= 1:
                        # Activate/renew the service
                        service.status = 'ACTIVE'
                        service.save(update_fields=['status'])

                        # Update customer status if needed
                        if customer.status in ('SUSPENDED', 'INACTIVE', 'PENDING'):
                            customer.status = 'ACTIVE'
                            customer.save(update_fields=['status'])

                        if radius_cred:
                            radius_cred.is_enabled = True
                            radius_cred.disabled_reason = ''
                            radius_cred.subscription_activated_at = timezone.now()
                            if new_expiry:
                                radius_cred.expiration_date = new_expiry
                            radius_cred.save()

                            # Sync to RADIUS tables
                            try:
                                radius_cred.sync_to_radius()
                            except Exception as e:
                                logger.error(f"RADIUS sync failed: {e}")

                        logger.info(
                            f"RENEWAL SUCCESS: customer={customer.customer_code} "
                            f"account={bill_ref} amount={amount} "
                            f"quantity={quantity} new_expiry={new_expiry}"
                        )

                        # Force MikroTik reconnect
                        self.trigger_mikrotik_reactivation(service)

                        # Send confirmation SMS
                        try:
                            # Build a simple renewal confirmation
                            _send_renewal_sms(customer, amount, quantity, new_expiry, msisdn)
                        except Exception as e:
                            logger.warning(f"Renewal SMS failed: {e}")

                    else:
                        # Partial payment — recorded but service not activated
                        logger.info(
                            f"PARTIAL PAYMENT: customer={customer.customer_code} "
                            f"account={bill_ref} amount={amount} — "
                            f"requires {service.plan.base_price if service.plan else 'N/A'} for activation"
                        )

                    logger.info(
                        f"C2B payment processed: {trans_id} | "
                        f"Customer: {customer.customer_code} | Amount: KES {amount} | "
                        f"Quantity: {quantity} period(s)"
                    )

            except Exception as e:
                logger.error(f"Internal Error in {target_tenant_schema}: {e}", exc_info=True)
                return Response(
                    {"ResultCode": 1, "ResultDesc": "Internal Error"},
                    status=status.HTTP_200_OK
                )

        return Response(
            {"ResultCode": 0, "ResultDesc": "Success"},
            status=status.HTTP_200_OK
        )


def _send_renewal_sms(customer, amount, quantity, new_expiry, phone_override=None):
    """Send a renewal confirmation SMS to the customer."""
    try:
        from apps.messaging.services.notification_sender import _send_once, _fmt_phone
        
        phone = phone_override or (
            customer.user.phone_number if customer.user else None
        )
        if not phone:
            return

        # Format the message
        period_str = f"{quantity} month{'s' if quantity > 1 else ''}" if quantity > 1 else "1 month"
        expiry_str = new_expiry.strftime('%d %b %Y') if new_expiry else 'unlimited'
        name = customer.user.first_name or 'Customer'
        
        message = (
            f"Hi {name}, payment of KES {amount:,.0f} received. "
            f"Your internet has been renewed for {period_str}. "
            f"Expires: {expiry_str}. Thank you!"
        )

        _send_once(
            f"c2b_renewal:{customer.id}:{int(amount)}",
            _fmt_phone(phone),
            message,
            ttl=3600
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Renewal SMS failed: {e}")


# URL patterns for webhooks (to be added to main urls.py)
# path('api/v1/webhooks/payhero/subscription/', PayHeroSubscriptionWebhookView.as_view()),
# path('api/v1/webhooks/payhero/hotspot/', PayHeroHotspotWebhookView.as_view()),
# path('api/v1/webhooks/payhero/billing/', PayHeroBillingWebhookView.as_view()),
# path('api/v1/webhooks/mpesa/c2b-callback/', MpesaC2BWebhookView.as_view()),