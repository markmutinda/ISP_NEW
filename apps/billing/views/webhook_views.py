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

            return mpesa_txn, created

        except IntegrityError:
            logger.info(f"Duplicate C2B transaction ignored at raw persist stage: {trans_id}")
            return MpesaTransaction.objects.filter(transaction_id=trans_id).first(), False

    def _calculate_renewal_expiry(self, service, amount, current_expiry=None, customer=None):
        """
        Calculates subscription parameters based on incoming payments.
        Supports seamless pre‑expiry day stacking and smart upgrade detection.
        
        Returns: (matched_plan, quantity, new_expiry)
        """
        from decimal import Decimal
        from django.utils import timezone
        from apps.billing.models.billing_models import Plan
        
        now = timezone.now()
        amount = Decimal(str(amount))
        existing_credit = Decimal(str(customer.prepaid_credit or 0)) if customer else Decimal('0')
        total_available = amount + existing_credit

        if not service.plan:
            return None, 0, None

        current_plan = service.plan
        current_plan_price = Decimal(str(current_plan.base_price or 0))
        current_plan_type = current_plan.plan_type
        matched_plan = None

        # 1. Intent Detection: Did they pay for a completely different high‑tier plan?
        if current_plan_price > 0:
            remainder = total_available % current_plan_price
            if remainder >= Decimal('0.50'):  # Not a direct renewal multiple
                matched_plan = Plan.objects.filter(
                    is_active=True,
                    plan_type=current_plan_type,
                    base_price=total_available
                ).first()

        # 2. Check for multi‑month clean renewals of their existing plan
        if not matched_plan and current_plan_price > 0:
            remainder = total_available % current_plan_price
            if remainder < Decimal('0.50') and total_available >= current_plan_price:
                matched_plan = current_plan

        # 3. Fallback to current plan if minimum threshold is met
        if not matched_plan and total_available >= current_plan_price:
            matched_plan = current_plan

        if not matched_plan:
            if customer:
                customer.prepaid_credit = total_available
                customer.save(update_fields=['prepaid_credit'])
            logger.info(f"Partial payment saved as prepaid credit: KES {total_available}")
            return None, 0, None

        plan_price = matched_plan.base_price
        quantity = int(total_available / plan_price)
        remainder = total_available - (plan_price * quantity)

        if customer:
            customer.prepaid_credit = remainder if remainder >= Decimal('0.50') else Decimal('0')
            customer.save(update_fields=['prepaid_credit'])

        # Time Stacking Logic: Accumulate days if current plan line is still active
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

        # 1. FIND THE TENANT
        target_tenant_schema = None
        fallback_tenant_schema = None
        
        if connection.schema_name == 'public':
            for tenant in Tenant.objects.exclude(schema_name='public'):
                with schema_context(tenant.schema_name):
                    try:
                        if MpesaConfiguration.objects.filter(
                            business_shortcode=shortcode
                        ).filter(
                            Q(is_active=True) | Q(c2b_urls_registered=True)
                        ).exists():
                            if ServiceConnection.objects.filter(
                                models.Q(billing_account_number__iexact=bill_ref) |
                                models.Q(mpesa_account_number__iexact=bill_ref)
                            ).exists():
                                target_tenant_schema = tenant.schema_name
                                logger.info(f"Found matching tenant via ServiceConnection: {tenant.schema_name}")
                                break
                            
                            from apps.billing.models.hotspot_models import HotspotSession
                            if HotspotSession.objects.filter(session_id__icontains=bill_ref).exists():
                                target_tenant_schema = tenant.schema_name
                                logger.info(f"Found matching tenant via HotspotSession reference: {tenant.schema_name}")
                                break
                            
                            if not fallback_tenant_schema:
                                fallback_tenant_schema = tenant.schema_name
                                logger.info(
                                    f"Shortcode {shortcode} matched tenant {tenant.schema_name} "
                                    f"(no account match for '{bill_ref}', will record as unmatched if no other match found)"
                                )
                    except Exception as e:
                        logger.debug(f"Error checking tenant {tenant.schema_name}: {e}")
                        continue
            
            if not target_tenant_schema and fallback_tenant_schema:
                target_tenant_schema = fallback_tenant_schema
                logger.info(f"Using fallback tenant {target_tenant_schema} for unmatched payment")
        else:
            target_tenant_schema = connection.schema_name

        if not target_tenant_schema:
            logger.warning(
                f"UNMATCHED PAYMENT: ID={trans_id}, Account={bill_ref}, SC={shortcode}. "
                "No tenant matched (no active or registered M-Pesa config found). Manual reconciliation required."
            )
            return Response(
                {"ResultCode": 0, "ResultDesc": "Account Not Found"},
                status=status.HTTP_200_OK
            )

        # 2. PROCESS INSIDE TENANT SCHEMA
        with schema_context(target_tenant_schema):
            # Check for existing completed records upfront
            existing = Payment.objects.filter(
                schema_name=target_tenant_schema,
                mpesa_receipt=trans_id,
                status='COMPLETED',
            ).first()
            if existing:
                logger.info(f"C2B {trans_id} already completed by STK callback, skipping")
                return Response(
                    {"ResultCode": 0, "ResultDesc": "Already processed"},
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

            if getattr(mpesa_txn, 'payment_id', None):
                logger.info(f"Duplicate C2B callback ignored: {trans_id}")
                return Response(
                    {"ResultCode": 0, "ResultDesc": "Duplicate"},
                    status=status.HTTP_200_OK
                )

            # Begin customer activation block safely
            try:
                with transaction.atomic():
                    # Find service connection structures
                    service = ServiceConnection.objects.filter(
                        models.Q(billing_account_number__iexact=bill_ref) |
                        models.Q(mpesa_account_number__iexact=bill_ref)
                    ).select_related('customer', 'plan', 'pppoe_user', 'hotspot_user').first()

                    hotspot_session = None
                    if not service:
                        from apps.billing.models.hotspot_models import HotspotSession
                        hotspot_session = HotspotSession.objects.filter(
                            session_id__icontains=bill_ref, status='pending'
                        ).select_related('plan', 'router').first()
                        
                        if hotspot_session:
                            logger.info(f"Found HotspotSession {hotspot_session.session_id} for payment")
                            method, _ = InvoiceItemPayment.objects.get_or_create(
                                method_type='MPESA_PAYBILL', schema_name=target_tenant_schema,
                                defaults={'name': 'M-Pesa Paybill', 'code': f'MPESA_PAYBILL_{target_tenant_schema[:10]}', 'is_active': True}
                            )
                            first_name = data.get('FirstName', '')
                            last_name = data.get('LastName', '')
                            payer_full_name = f"{first_name} {last_name}".strip()

                            payment = Payment.objects.create(
                                amount=amount, payment_method=method, status='COMPLETED',
                                transaction_id=trans_id, mpesa_receipt=trans_id, mpesa_phone=msisdn,
                                payer_phone='', payer_name=payer_full_name if payer_full_name else "M-Pesa User",
                                mpesa_transaction=mpesa_txn, payment_date=timezone.now(),
                                schema_name=target_tenant_schema, hotspot_session=hotspot_session,
                                notes=f"C2B hotspot payment via Paybill. Session: {hotspot_session.session_id}. Ref: {trans_id}"
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

                        payment = Payment.objects.create(
                            customer=None, amount=amount, payment_method=method, status='COMPLETED',
                            transaction_id=trans_id, mpesa_receipt=trans_id, mpesa_phone=msisdn, payer_phone='',
                            payer_name=payer_full_name or "M-Pesa User", mpesa_transaction=mpesa_txn,
                            payment_date=timezone.now(), schema_name=target_tenant_schema,
                            notes=f"UNMATCHED ACCOUNT: Customer entered '{bill_ref}'. Manual activation required."
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

                    payment = Payment.objects.create(
                        customer=service.customer, amount=amount, payment_method=method, status='COMPLETED',
                        transaction_id=trans_id, mpesa_receipt=trans_id, mpesa_phone=msisdn, payer_phone='',
                        payer_name=payer_full_name if payer_full_name else "M-Pesa User", mpesa_transaction=mpesa_txn,
                        payment_date=timezone.now(), schema_name=target_tenant_schema,
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
                        
                        try:
                            # Use the proper SMSNotifier method that respects toggles
                            from apps.messaging.services.notification_sender import SMSNotifier
                            SMSNotifier.pppoe_renewal(
                                customer=customer,
                                plan_name=matched_plan.name if matched_plan else '',
                                new_expiry=new_expiry,
                                amount_paid=amount,
                            )
                        except Exception as e:
                            logger.warning(f"SMS notice skipped: {e}")
                    else:
                        logger.info(f"Partial payment processed for customer: {customer.customer_code}")

                    logger.info(
                        f"C2B payment processed: {trans_id} | "
                        f"Customer: {customer.customer_code} | Amount: KES {amount} | "
                        f"Quantity: {quantity} period(s) | Credit balance: {customer.prepaid_credit}"
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