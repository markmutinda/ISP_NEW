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
from django.core.cache import cache  # ← ADDED: For cross-process locking
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
    - Unrecognized account → recorded for manual reconciliation
    
    POST /api/v1/webhooks/mpesa/c2b-callback/
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def _persist_c2b_transaction(self, *, config, trans_id, amount, msisdn, bill_ref, data, schema_name):
        """
        Persist the raw C2B transaction before customer activation work.
        Intentionally runs outside transaction.atomic() so that a failed activation
        does not delete evidence that a customer paid.
        
        Returns:
            tuple: (mpesa_txn, created_or_handled)
                - created_or_handled: True if newly created, False if existing without payment,
                  None if already fully processed (has payment linked)
        """
        try:
            mpesa_txn, created = MpesaTransaction.objects.get_or_create(
                transaction_id=trans_id,
                defaults={
                    "configuration": config,
                    "merchant_request_id": f"C2B-{trans_id}",
                    "checkout_request_id": f"C2B-{trans_id}",
                    "transaction_type": "C2B",
                    "amount": amount,
                    "phone_number": msisdn,
                    "account_reference": bill_ref,
                    "status": "COMPLETED",
                    "callback_data": data,
                    "callback_received_at": timezone.now(),
                    "schema_name": schema_name,
                },
            )

            if not created:
                # Update payload fields for auditing/debugging if retried by Safaricom
                update_fields = []
                if not mpesa_txn.callback_data:
                    mpesa_txn.callback_data = data
                    update_fields.append("callback_data")
                if not mpesa_txn.callback_received_at:
                    mpesa_txn.callback_received_at = timezone.now()
                    update_fields.append("callback_received_at")
                if mpesa_txn.status != "COMPLETED":
                    mpesa_txn.status = "COMPLETED"
                    update_fields.append("status")

                if update_fields:
                    mpesa_txn.save(update_fields=update_fields)

            # Signal to caller if this was already fully processed (has payment linked)
            already_processed = mpesa_txn.payment_id is not None
            return mpesa_txn, None if already_processed else created

        except IntegrityError:
            logger.info(f"Duplicate C2B transaction ignored at raw persist stage: {trans_id}")
            existing = MpesaTransaction.objects.filter(transaction_id=trans_id).first()
            if existing:
                # Signal to caller if this was already fully processed (has payment linked)
                already_processed = existing.payment_id is not None
                return existing, None if already_processed else False
            return None, False

    def _calculate_renewal_expiry(self, service, amount, current_expiry=None, customer=None):
        """
        Only activates if the payment amount exactly matches:
          - the current plan's price (or an exact multiple of it, for stacking), OR
          - another active plan's price (exact match, for a genuine upgrade/downgrade)
        Anything else is recorded as unmatched — no auto-activation, credit not touched.
        
        Returns: (matched_plan, quantity, new_expiry)
        """
        from decimal import Decimal
        from django.utils import timezone
        from apps.billing.models.billing_models import Plan

        now = timezone.now()
        amount = Decimal(str(amount))

        if not service.plan:
            return None, 0, None

        current_plan = service.plan
        current_price = Decimal(str(current_plan.base_price or 0))

        matched_plan = None
        quantity = 0

        # 1. Exact match to current plan price (or clean multiple of it — stacking)
        if current_price > 0 and amount % current_price == 0:
            matched_plan = current_plan
            quantity = int(amount / current_price)

        # 2. Exact match to a different active plan's price (upgrade/downgrade)
        if not matched_plan:
            matched_plan = Plan.objects.filter(
                is_active=True,
                plan_type=current_plan.plan_type,
                base_price=amount,
            ).first()
            if matched_plan:
                quantity = 1

        if not matched_plan:
            # No exact match — do NOT touch prepaid_credit, do NOT activate.
            # Payment is still recorded by the caller; this just signals "needs manual review".
            return None, 0, None

        # ─── NEW: CALENDAR_MONTH plans always renew as exactly 1 cycle ──────────
        if matched_plan.validity_type == 'CALENDAR_MONTH':
            from utils.billing_dates import resolve_calendar_renewal
            
            # Get the customer's radius credentials to access billing_anchor_day
            radius_cred = getattr(customer, 'radius_credentials', None) if customer else None
            prior_anchor = getattr(radius_cred, 'billing_anchor_day', None)
            
            # Use resolve_calendar_renewal to handle on-time vs late payment
            _, new_expiry = resolve_calendar_renewal(
                current_expiry,
                anchor_day=prior_anchor,
                now=now
            )
            
            # Note: quantity is always 1 for CALENDAR_MONTH (no stacking)
            return matched_plan, 1, new_expiry
        # ──────────────────────────────────────────────────────────────────────────

        plan_price = matched_plan.base_price
        start_time = current_expiry if (current_expiry and current_expiry > now) else now
        validity_delta = matched_plan.get_validity_timedelta()
        new_expiry = start_time + (validity_delta * quantity) if validity_delta else None

        return matched_plan, quantity, new_expiry

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

        # ── NEW: cross-process lock so a concurrent STK callback for the
        # same receipt can't race this C2B confirmation ──
        lock_key = f"mpesa_receipt_lock:{trans_id}"
        if not cache.add(lock_key, "1", timeout=25):
            logger.info(
                f"C2B webhook: receipt {trans_id} already being processed "
                "(likely the matching STK callback), skipping duplicate"
            )
            return Response(
                {"ResultCode": 0, "ResultDesc": "Already processing"},
                status=status.HTTP_200_OK
            )

        try:
            # ═══════════════════════════════════════════════════════════════
            # 1. FIND THE TENANT — O(1) lookup instead of scanning all schemas
            # ═══════════════════════════════════════════════════════════════
            target_tenant_schema = None

            if connection.schema_name == 'public':
                from apps.core.models import MpesaShortcodeTenantMap
                mapping = MpesaShortcodeTenantMap.objects.filter(
                    business_shortcode=shortcode,
                    is_active=True
                ).values_list('schema_name', flat=True).first()
                target_tenant_schema = mapping
                if target_tenant_schema:
                    logger.info(f"C2B shortcode map hit: {shortcode} -> {target_tenant_schema}")
                else:
                    logger.warning(f"No tenant mapping found for shortcode {shortcode}")
            else:
                target_tenant_schema = connection.schema_name

            if not target_tenant_schema:
                logger.warning(
                    f"UNMATCHED PAYMENT: ID={trans_id}, Account={bill_ref}, SC={shortcode}. "
                    "No tenant matched (no active M-Pesa shortcode mapping found). Manual reconciliation required."
                )
                return Response(
                    {"ResultCode": 0, "ResultDesc": "Account Not Found"},
                    status=status.HTTP_200_OK
                )

            # 2. PROCESS INSIDE TENANT SCHEMA
            with schema_context(target_tenant_schema):
                # ============================================================
                # FIX 1: Check if already processed by STK callback path
                # ============================================================
                # Check 1: Payment record already completed
                existing_payment = Payment.objects.filter(
                    schema_name=target_tenant_schema,
                    mpesa_receipt=trans_id,
                    status='COMPLETED',
                ).first()
                if existing_payment:
                    logger.info(f"C2B {trans_id} already completed by STK callback (payment={existing_payment.id}), skipping")
                    return Response(
                        {"ResultCode": 0, "ResultDesc": "Already processed"},
                        status=status.HTTP_200_OK
                    )
                
                # Check 2: MpesaTransaction already has a payment linked (STK path)
                existing_txn = MpesaTransaction.objects.filter(
                    transaction_id=trans_id,
                    status='COMPLETED',
                ).first()
                if existing_txn and existing_txn.payment_id:
                    logger.info(
                        f"C2B {trans_id} already processed via STK callback "
                        f"(payment={existing_txn.payment_id}), skipping C2B duplicate"
                    )
                    return Response(
                        {"ResultCode": 0, "ResultDesc": "Already processed via STK"},
                        status=status.HTTP_200_OK
                    )

                # Look up configurations cleanly before entering the atomic loop
                config = MpesaConfiguration.objects.filter(
                    business_shortcode=shortcode
                ).filter(
                    Q(is_active=True) | Q(c2b_urls_registered=True)
                ).first()

                if not config:
                    logger.error(f"No M-Pesa config (active or registered) for shortcode {shortcode}")
                    return Response(
                        {"ResultCode": 1, "ResultDesc": "Configuration Error"},
                        status=status.HTTP_200_OK
                    )

                # 🛡️ PERSIST IMMEDIATELY (Outside the risky activation block)
                mpesa_txn, created = self._persist_c2b_transaction(
                    config=config, trans_id=trans_id, amount=amount, msisdn=msisdn,
                    bill_ref=bill_ref, data=data, schema_name=target_tenant_schema
                )

                if not mpesa_txn:
                    logger.error(f"Could not persist raw C2B transaction: {trans_id}")
                    return Response(
                        {"ResultCode": 1, "ResultDesc": "Could not persist transaction"},
                        status=status.HTTP_200_OK
                    )

                # If created is None, it means already fully processed (has payment linked)
                if created is None:
                    logger.info(f"C2B {trans_id} already fully processed (STK path), skipping")
                    return Response(
                        {"ResultCode": 0, "ResultDesc": "Already processed"},
                        status=status.HTTP_200_OK
                    )

                if getattr(mpesa_txn, 'payment_id', None):
                    logger.info(f"Duplicate C2B callback ignored: {trans_id}")
                    return Response(
                        {"ResultCode": 0, "ResultDesc": "Duplicate"},
                        status=status.HTTP_200_OK
                    )

                # Begin customer activation block safely
                try:
                    with transaction.atomic():
                        # ═══════════════════════════════════════════════════════════════
                        # FIND SERVICE CONNECTION — EXACT MATCH (no __iexact overhead)
                        # ═══════════════════════════════════════════════════════════════
                        service = ServiceConnection.objects.filter(
                            models.Q(billing_account_number=bill_ref) |
                            models.Q(mpesa_account_number=bill_ref)
                        ).select_related('customer', 'plan', 'pppoe_user', 'hotspot_user').first()

                        hotspot_session = None
                        if not service:
                            from apps.billing.models.hotspot_models import HotspotSession
                            # FIX: Match on session_id regardless of status, so C2B confirmations
                            # arriving after STK already activated the session still link the FK
                            hotspot_session = HotspotSession.objects.filter(
                                session_id__icontains=bill_ref
                            ).select_related('plan', 'router').first()
                            
                            if hotspot_session:
                                # ============================================================
                                # FIX 2: Check if already activated by STK callback
                                # ============================================================
                                hotspot_session.refresh_from_db()
                                if hotspot_session.status in ('active', 'paid'):
                                    logger.info(
                                        f"HotspotSession {hotspot_session.session_id} already processed via STK, "
                                        "skipping C2B"
                                    )
                                    # Link the existing payment if found
                                    existing_payment = Payment.objects.filter(
                                        hotspot_session=hotspot_session,
                                        status='COMPLETED'
                                    ).first()
                                    if existing_payment:
                                        mpesa_txn.payment = existing_payment
                                        mpesa_txn.save(update_fields=['payment'])
                                    return Response(
                                        {"ResultCode": 0, "ResultDesc": "Already processed"},
                                        status=status.HTTP_200_OK
                                    )
                                
                                logger.info(f"Found HotspotSession {hotspot_session.session_id} for payment")
                                method, _ = InvoiceItemPayment.objects.get_or_create(
                                    method_type='MPESA_PAYBILL', schema_name=target_tenant_schema,
                                    defaults={'name': 'M-Pesa Paybill', 'code': f'MPESA_PAYBILL_{target_tenant_schema[:10]}', 'is_active': True}
                                )
                                first_name = data.get('FirstName', '')
                                last_name = data.get('LastName', '')
                                payer_full_name = f"{first_name} {last_name}".strip()

                                # FIX 4: C2B hotspot-matched branch - set service_type='HOTSPOT'
                                payment = Payment.objects.create(
                                    amount=amount, payment_method=method, status='COMPLETED',
                                    transaction_id=trans_id, mpesa_receipt=trans_id, mpesa_phone=msisdn,
                                    payer_phone='', payer_name=payer_full_name if payer_full_name else "M-Pesa User",
                                    mpesa_transaction=mpesa_txn, payment_date=timezone.now(),
                                    schema_name=target_tenant_schema, hotspot_session=hotspot_session,
                                    notes=f"C2B hotspot payment via Paybill. Session: {hotspot_session.session_id}. Ref: {trans_id}",
                                    service_type='HOTSPOT',   # Permanent classification for analytics
                                )
                                mpesa_txn.payment = payment
                                mpesa_txn.save(update_fields=['payment'])
                                hotspot_session.mark_paid(trans_id)
                                return Response({"ResultCode": 0, "ResultDesc": "Success"}, status=status.HTTP_200_OK)

                        # Unmatched account tracking
                        if not service:
                            logger.warning(f"UNMATCHED ACCOUNT: ID={trans_id}, Account={bill_ref}, SC={shortcode}. Recording for manual reconciliation.")
                            method, _ = InvoiceItemPayment.objects.get_or_create(
                                method_type='MPESA_PAYBILL', schema_name=target_tenant_schema,
                                defaults={'name': 'M-Pesa Paybill', 'code': f'MPESA_PAYBILL_{target_tenant_schema[:10]}', 'is_active': True}
                            )
                            first_name = data.get('FirstName', '')
                            last_name = data.get('LastName', '')
                            payer_full_name = f"{first_name} {last_name}".strip()

                            # FIX 4: Unmatched-account branch - tag as HOTSPOT if ref looks like one, else OTHER
                            is_hotspot_ref = bill_ref.upper().startswith("HS_") or "-" in bill_ref[:8]
                            
                            payment = Payment.objects.create(
                                customer=None, amount=amount, payment_method=method, status='COMPLETED',
                                transaction_id=trans_id, mpesa_receipt=trans_id, mpesa_phone=msisdn, payer_phone='',
                                payer_name=payer_full_name or "M-Pesa User", mpesa_transaction=mpesa_txn,
                                payment_date=timezone.now(), schema_name=target_tenant_schema,
                                notes=f"UNMATCHED ACCOUNT: Customer entered '{bill_ref}'. Manual activation required.",
                                service_type='HOTSPOT' if is_hotspot_ref else 'OTHER',  # Permanent classification
                            )
                            mpesa_txn.payment = payment
                            mpesa_txn.save(update_fields=['payment'])
                            return Response(
                                {"ResultCode": 0, "ResultDesc": "Success - Recorded for Manual Reconciliation"},
                                status=status.HTTP_200_OK
                            )

                        # Matched subscription processing sequence
                        method, _ = InvoiceItemPayment.objects.get_or_create(
                            method_type='MPESA_PAYBILL', schema_name=target_tenant_schema,
                            defaults={'name': 'M-Pesa Paybill', 'code': f'MPESA_PAYBILL_{target_tenant_schema[:10]}', 'is_active': True}
                        )
                        first_name = data.get('FirstName', '')
                        last_name = data.get('LastName', '')
                        payer_full_name = f"{first_name} {last_name}".strip()

                        # FIX 4: Matched PPPoE-subscription branch - set service_type='PPPOE'
                        payment = Payment.objects.create(
                            customer=service.customer, amount=amount, payment_method=method, status='COMPLETED',
                            transaction_id=trans_id, mpesa_receipt=trans_id, mpesa_phone=msisdn, payer_phone='',
                            payer_name=payer_full_name if payer_full_name else "M-Pesa User", mpesa_transaction=mpesa_txn,
                            payment_date=timezone.now(), schema_name=target_tenant_schema,
                            notes=f"C2B payment via Paybill. Account: {bill_ref}. Ref: {trans_id}",
                            service_type='PPPOE',   # Permanent classification for analytics
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
                        radius_cred = CustomerRadiusCredentials.objects.filter(customer=customer).first()
                        current_expiry = radius_cred.expiration_date if radius_cred else None
                        original_plan_id = service.plan_id  

                        matched_plan, quantity, new_expiry = self._calculate_renewal_expiry(
                            service, amount, current_expiry, customer=customer
                        )
                        
                        if quantity >= 1:
                            from apps.billing.models.subscription_models import Subscription
                            from django.db import connection as db_conn
                            
                            plan_changed = (matched_plan.id != original_plan_id) if matched_plan else False

                            Subscription.objects.filter(customer=customer, status='ACTIVE').update(status='EXPIRED')

                            new_subscription = Subscription.objects.create(
                                customer=customer,
                                service_connection=service,
                                plan=matched_plan,
                                payment=payment,
                                amount_paid=amount,
                                status='ACTIVE',
                                started_at=timezone.now(),
                                expires_at=new_expiry,
                                schema_name=db_conn.schema_name,
                            )

                            service.status = 'ACTIVE'
                            service.save(update_fields=['status'])
                            if customer.status in ('SUSPENDED', 'INACTIVE', 'PENDING'):
                                customer.status = 'ACTIVE'
                                customer.save(update_fields=['status'])

                            if radius_cred:
                                radius_cred.is_enabled = True
                                radius_cred.disabled_reason = ''
                                radius_cred.subscription_activated_at = timezone.now()
                                if new_expiry:
                                    radius_cred.expiration_date = new_expiry
                                
                                # ─── NEW: Update billing_anchor_day for CALENDAR_MONTH ──────────
                                # The _calculate_renewal_expiry method already handled the
                                # anchor resolution via resolve_calendar_renewal, but we need
                                # to store the updated anchor on the credentials
                                if matched_plan and matched_plan.validity_type == 'CALENDAR_MONTH':
                                    from utils.billing_dates import resolve_calendar_renewal
                                    new_anchor, _ = resolve_calendar_renewal(
                                        current_expiry,
                                        anchor_day=radius_cred.billing_anchor_day,
                                        now=timezone.now()
                                    )
                                    radius_cred.billing_anchor_day = new_anchor
                                    logger.info(
                                        f"Updated billing_anchor_day={new_anchor} for {radius_cred.username} "
                                        "(CALENDAR_MONTH renewal via C2B)"
                                    )
                                # ───────────────────────────────────────────────────────────────────
                                
                                if plan_changed:
                                    try:
                                        from apps.radius.signals_auto_sync import _get_or_create_bandwidth_profile
                                        service.plan = matched_plan
                                        new_profile = _get_or_create_bandwidth_profile(service)
                                        if new_profile:
                                            radius_cred.bandwidth_profile = new_profile
                                    except Exception as bp_err:
                                        logger.warning(f"Bandwidth profile allocation failed: {bp_err}")
                                
                                radius_cred.save()
                                
                                # ============================================================
                                # FIX: Clear old expiry reminder logs to prevent duplicate reminders
                                # This ensures a fresh reminder cycle starts after renewal
                                # ============================================================
                                try:
                                    from apps.radius.models import RadiusExpiryReminderLog
                                    deleted_count = RadiusExpiryReminderLog.objects.filter(
                                        customer_id=str(customer.id)
                                    ).delete()
                                    
                                    if deleted_count[0] > 0:
                                        logger.info(
                                            f"[C2B] Cleared {deleted_count[0]} old expiry reminder logs "
                                            f"for customer {customer.customer_code} after renewal"
                                        )
                                except Exception as log_clear_err:
                                    logger.warning(f"Failed to clear old reminder logs (non-fatal): {log_clear_err}")
                                
                                try:
                                    radius_cred.sync_to_radius()
                                except Exception as e:
                                    logger.error(f"FreeRADIUS cluster sync failed: {e}")

                                try:
                                    from apps.radius.services.coa_service import CoAService
                                    coa = CoAService()
                                    router_ip = radius_cred.router.vpn_ip_address or radius_cred.router.ip_address if radius_cred.router else None
                                    if router_ip:
                                        coa.disconnect_user(username=radius_cred.username, nas_ip_address=router_ip)
                                        logger.info(f"CoA session reset sent via NAS IP: {router_ip}")
                                except Exception as coa_err:
                                    logger.warning(f"CoA disconnection bypassed (non-fatal): {coa_err}")

                            if plan_changed:
                                service.plan = matched_plan
                                service.monthly_price = matched_plan.base_price
                                service.download_speed = matched_plan.download_speed or service.download_speed
                                service.upload_speed = matched_plan.upload_speed or service.upload_speed
                                service.save(update_fields=['plan', 'monthly_price', 'download_speed', 'upload_speed'])

                            self.trigger_mikrotik_reactivation(service)
                            
                            # ============================================================
                            # FIX: Use only pppoe_renewal (which now checks pppoe_payment_confirmation)
                            # No duplicate SMS calls here - the renewal method handles the merged notification
                            # ============================================================
                            try:
                                from apps.messaging.services.notification_sender import SMSNotifier
                                # The pppoe_renewal method now checks pppoe_payment_confirmation toggle
                                # Pass the M-Pesa transaction ID as reference so it appears in the SMS
                                SMSNotifier.pppoe_renewal(
                                    customer=customer,
                                    plan_name=matched_plan.name if matched_plan else '',
                                    expires_at=new_expiry,
                                    reference=trans_id,  # <-- ADDED: Pass M-Pesa transaction ID as reference
                                    schema_name=target_tenant_schema
                                )
                                logger.info(f"PPPoE renewal SMS sent to customer {customer.id} with reference {trans_id}")
                            except Exception as e:
                                logger.warning(f"PPPoE renewal SMS failed for customer {customer.id}: {e}")
                        else:
                            # ============================================================
                            # FIX 3: UNMATCHED AMOUNT — flag clearly for manual reconciliation
                            # ============================================================
                            logger.warning(
                                f"UNMATCHED AMOUNT: customer={customer.customer_code}, amount={amount}, "
                                f"current_plan={service.plan.name if service.plan else None}. "
                                f"No plan price matched — payment recorded but NOT activated. Manual activation required."
                            )
                            payment.notes = (payment.notes or '') + (
                                f"\nUnmatched amount KES {amount} — no plan price match. Manual activation required."
                            )
                            payment.save(update_fields=['notes'])

                        logger.info(
                            f"C2B payment processed: {trans_id} | "
                            f"Customer: {customer.customer_code} | Amount: KES {amount} | "
                            f"Quantity: {quantity} period(s)"
                        )

                except Exception as e:
                    # 🛡️ CRITICAL EXCEPTION GATEKEEPER
                    logger.error(f"Internal Error in {target_tenant_schema}: {e}", exc_info=True)
                    try:
                        MpesaTransaction.objects.filter(transaction_id=trans_id).update(
                            status="FAILED",
                            result_desc=f"C2B activation failed after receipt: {str(e)[:500]}",
                            callback_received_at=timezone.now(),
                        )
                    except Exception as log_err:
                        logger.error(f"Could not mark C2B transaction as failed: {log_err}", exc_info=True)

                    return Response(
                        {"ResultCode": 0, "ResultDesc": "Payment received - activation failed, manual reconciliation required"},
                        status=status.HTTP_200_OK
                    )

            return Response({"ResultCode": 0, "ResultDesc": "Success"}, status=status.HTTP_200_OK)
        
        finally:
            # ── NEW: Always release the lock ──
            cache.delete(lock_key)