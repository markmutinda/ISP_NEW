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

        # 1. FIND THE TENANT (UPDATED: shortcode match alone is sufficient)
        target_tenant_schema = None
        fallback_tenant_schema = None  # Track tenant with shortcode-only match
        
        if connection.schema_name == 'public':
            for tenant in Tenant.objects.exclude(schema_name='public'):
                with schema_context(tenant.schema_name):
                    try:
                        if MpesaConfiguration.objects.filter(
                            business_shortcode=shortcode, is_active=True
                        ).exists():
                            # Prefer tenant where the account reference matches a ServiceConnection
                            if ServiceConnection.objects.filter(
                                models.Q(billing_account_number__iexact=bill_ref) |
                                models.Q(mpesa_account_number__iexact=bill_ref)
                            ).exists():
                                target_tenant_schema = tenant.schema_name
                                logger.info(f"Found matching tenant via ServiceConnection: {tenant.schema_name}")
                                break
                            
                            # Check HotspotSession
                            from apps.billing.models.hotspot_models import HotspotSession
                            if HotspotSession.objects.filter(session_id__icontains=bill_ref).exists():
                                target_tenant_schema = tenant.schema_name
                                logger.info(f"Found matching tenant via HotspotSession reference: {tenant.schema_name}")
                                break
                            
                            # Shortcode matches but no account match — store as fallback candidate
                            if not fallback_tenant_schema:
                                fallback_tenant_schema = tenant.schema_name
                                logger.info(
                                    f"Shortcode {shortcode} matched tenant {tenant.schema_name} "
                                    f"(no account match for '{bill_ref}', will record as unmatched if no other match found)"
                                )
                    except Exception as e:
                        logger.debug(f"Error checking tenant {tenant.schema_name}: {e}")
                        continue
            
            # If no perfect match found but we have a fallback, use it
            if not target_tenant_schema and fallback_tenant_schema:
                target_tenant_schema = fallback_tenant_schema
                logger.info(f"Using fallback tenant {target_tenant_schema} for unmatched payment")
        else:
            target_tenant_schema = connection.schema_name

        if not target_tenant_schema:
            logger.warning(
                f"UNMATCHED PAYMENT: ID={trans_id}, Account={bill_ref}, SC={shortcode}. "
                "No tenant matched (no active M-Pesa config found). Manual reconciliation required."
            )
            # Still record the payment at public schema level as completely unmatched?
            # For now, just return as before
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

                            # Extract names safely from the C2B data
                            first_name = data.get('FirstName', '')
                            last_name = data.get('LastName', '')
                            payer_full_name = f"{first_name} {last_name}".strip()

                            # --- Record payment with improved payer name handling ---
                            payment = Payment.objects.create(
                                amount=amount,
                                payment_method=method,
                                status='COMPLETED',
                                transaction_id=trans_id,
                                mpesa_receipt=trans_id,
                                # Keep the hash in mpesa_phone for auditing/tracking if needed
                                mpesa_phone=msisdn,
                                # Clear payer_phone to prevent the UI from displaying the hash
                                payer_phone='',
                                # Use real names provided by Safaricom
                                payer_name=payer_full_name if payer_full_name else "M-Pesa User",
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

                    # --- If no service AND no hotspot session found, record as UNMATCHED for manual reconciliation ---
                    if not service:
                        logger.warning(
                            f"UNMATCHED ACCOUNT: ID={trans_id}, Account={bill_ref}, SC={shortcode}, "
                            f"tenant={target_tenant_schema}. Recording for manual reconciliation."
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

                        # --- Record transaction (unmatched) ---
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
                            logger.info(f"Duplicate unmatched C2B callback ignored: {trans_id}")
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

                        # Extract names safely from the C2B data
                        first_name = data.get('FirstName', '')
                        last_name = data.get('LastName', '')
                        payer_full_name = f"{first_name} {last_name}".strip()

                        # --- Record unmatched payment with clear notes for ISP ---
                        payment = Payment.objects.create(
                            customer=None,
                            amount=amount,
                            payment_method=method,
                            status='COMPLETED',
                            transaction_id=trans_id,
                            mpesa_receipt=trans_id,
                            mpesa_phone=msisdn,
                            payer_phone='',
                            payer_name=payer_full_name or "M-Pesa User",
                            mpesa_transaction=mpesa_txn,
                            payment_date=timezone.now(),
                            schema_name=target_tenant_schema,
                            notes=(
                                f"UNMATCHED ACCOUNT: Customer entered '{bill_ref}' which does not match "
                                f"any registered billing account or pending hotspot session. "
                                f"Manual activation required. TransID: {trans_id}, Phone: {msisdn}"
                            )
                        )
                        mpesa_txn.payment = payment
                        mpesa_txn.save(update_fields=['payment'])

                        logger.info(
                            f"Unmatched payment recorded for reconciliation: {trans_id} | "
                            f"Account: {bill_ref} | Amount: KES {amount} | Tenant: {target_tenant_schema}"
                        )
                        
                        return Response(
                            {"ResultCode": 0, "ResultDesc": "Success - Recorded for Manual Reconciliation"},
                            status=status.HTTP_200_OK
                        )

                    # --- Continue with normal matched service payment flow ---
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

                    # Extract names safely from the C2B data
                    first_name = data.get('FirstName', '')
                    last_name = data.get('LastName', '')
                    payer_full_name = f"{first_name} {last_name}".strip()

                    # --- Record payment with improved payer name handling ---
                    payment = Payment.objects.create(
                        customer=service.customer,
                        amount=amount,
                        payment_method=method,
                        status='COMPLETED',
                        transaction_id=trans_id,
                        mpesa_receipt=trans_id,
                        # Keep the hash in mpesa_phone for auditing/tracking if needed
                        mpesa_phone=msisdn,
                        # Clear payer_phone to prevent the UI from displaying the hash
                        payer_phone='',
                        # Use real names provided by Safaricom
                        payer_name=payer_full_name if payer_full_name else "M-Pesa User",
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

                    # --- PLAN-BASED QUANTITY RENEWAL (Claude Snapshot Fix) ---
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
                        
                        # Compute plan change flag accurately
                        plan_changed = (matched_plan.id != original_plan_id) if matched_plan else False

                        # --- (a) Expire any currently active logging rows ---
                        Subscription.objects.filter(customer=customer, status='ACTIVE').update(status='EXPIRED')

                        # --- (b) Build fresh core Subscription record ---
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

                        # --- (c) Activate core service metrics ---
                        service.status = 'ACTIVE'
                        service.save(update_fields=['status'])
                        if customer.status in ('SUSPENDED', 'INACTIVE', 'PENDING'):
                            customer.status = 'ACTIVE'
                            customer.save(update_fields=['status'])

                        # --- (d) Synchronize RADIUS provisioning profiles ---
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
                            
                            try:
                                radius_cred.sync_to_radius()
                            except Exception as e:
                                logger.error(f"FreeRADIUS cluster sync failed: {e}")

                            # --- (e) Deliver CoA Session Drop targeting explicit Router Gateway IP ---
                            try:
                                from apps.radius.services.coa_service import CoAService
                                coa = CoAService()
                                router_ip = radius_cred.router.vpn_ip_address or radius_cred.router.ip_address if radius_cred.router else None
                                if router_ip:
                                    coa.disconnect_user(username=radius_cred.username, nas_ip_address=router_ip)
                                    logger.info(f"CoA session reset sent via NAS IP: {router_ip}")
                            except Exception as coa_err:
                                logger.warning(f"CoA disconnection bypassed (non-fatal): {coa_err}")

                        # If they upgraded or changed tiers, synchronize core connection fields
                        if plan_changed:
                            service.plan = matched_plan
                            service.monthly_price = matched_plan.base_price
                            service.download_speed = matched_plan.download_speed or service.download_speed
                            service.upload_speed = matched_plan.upload_speed or service.upload_speed
                            service.save(update_fields=['plan', 'monthly_price', 'download_speed', 'upload_speed'])

                        self.trigger_mikrotik_reactivation(service)
                        
                        try:
                            _send_renewal_sms(customer, amount, quantity, new_expiry, msisdn)
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