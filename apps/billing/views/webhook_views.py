"""
M-Pesa C2B Webhook Handler

PUBLIC ENDPOINT - Receives callbacks from Safaricom when customers pay via Paybill.

Webhook:
- /api/v1/webhooks/mpesa/c2b-callback/ - Direct M-Pesa Paybill payments (Money -> Active Internet)
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

from apps.customers.models import ServiceConnection
from apps.billing.models.payment_models import MpesaConfiguration, Payment, MpesaTransaction, InvoiceItemPayment
from apps.network.integrations.mikrotik_api import MikrotikAPI
from apps.core.models import Tenant

logger = logging.getLogger(__name__)


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
                            # 🧠 Check home subscription accounts first
                            if ServiceConnection.objects.filter(
                                models.Q(billing_account_number__iexact=bill_ref) |
                                models.Q(mpesa_account_number__iexact=bill_ref)
                            ).exists():
                                target_tenant_schema = tenant.schema_name
                                logger.info(f"Found matching tenant via ServiceConnection: {tenant.schema_name}")
                                break
                            
                            # 🧠 Fallback: Check if this reference belongs to a temporary Hotspot Session
                            from apps.billing.models.hotspot_models import HotspotSession
                            if HotspotSession.objects.filter(session_id__icontains=bill_ref).exists():
                                target_tenant_schema = tenant.schema_name
                                logger.info(f"Found matching tenant via HotspotSession reference: {tenant.schema_name}")
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
                    # --- Find service (try ServiceConnection first, then HotspotSession) ---
                    service = ServiceConnection.objects.filter(
                        models.Q(billing_account_number__iexact=bill_ref) |
                        models.Q(mpesa_account_number__iexact=bill_ref)
                    ).select_related(
                        'customer', 'plan',
                        'pppoe_user', 'hotspot_user'
                    ).first()

                    # If no ServiceConnection found, check HotspotSession
                    hotspot_session = None
                    if not service:
                        from apps.billing.models.hotspot_models import HotspotSession
                        hotspot_session = HotspotSession.objects.filter(
                            session_id__icontains=bill_ref,
                            status='pending'
                        ).select_related('plan', 'router').first()
                        
                        if hotspot_session:
                            logger.info(f"Found HotspotSession {hotspot_session.session_id} for payment")
                            
                            # For hotspot sessions, we need to find or create a customer context
                            # Hotspot sessions may not have a full Customer record
                            # We'll process the payment and activate the session directly
                            
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
                                hotspot_session=hotspot_session,
                                notes=f"C2B hotspot payment via Paybill. Session: {hotspot_session.session_id}. Ref: {trans_id}"
                            )
                            mpesa_txn.payment = payment
                            mpesa_txn.save(update_fields=['payment'])

                            # --- Mark hotspot session as paid and activate ---
                            hotspot_session.mark_paid(trans_id)
                            
                            logger.info(
                                f"Hotspot payment processed: {trans_id} | "
                                f"Session: {hotspot_session.session_id} | Amount: KES {amount}"
                            )
                            
                            return Response(
                                {"ResultCode": 0, "ResultDesc": "Success"},
                                status=status.HTTP_200_OK
                            )

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