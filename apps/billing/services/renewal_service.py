"""
Manual subscription renewal — behaves like a completed payment landing.
Shared by the admin "Renew Subscription" action.
"""
import logging
import secrets
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


class RenewalError(Exception):
    """Expected, user-facing problem (maps to HTTP 400)."""


def _pick_service(customer_id):
    from apps.customers.models import ServiceConnection
    # Query by customer_id (NOT customer.services) so signals lazy-load a FRESH customer
    qs = ServiceConnection.objects.filter(customer_id=customer_id, plan__isnull=False).select_related('plan')
    return (
        qs.filter(status='ACTIVE').order_by('-created_at').first()
        or qs.filter(status='SUSPENDED').order_by('-created_at').first()
    )


def _compute_expiry(plan, creds, now):
    """Returns (new_expiry | None, new_anchor_day | None)."""
    current = creds.expiration_date
    vtype = (plan.validity_type or 'DAYS').upper()

    if vtype == 'CALENDAR_MONTH':
        from utils.billing_dates import resolve_calendar_renewal
        anchor, expiry = resolve_calendar_renewal(current, anchor_day=creds.billing_anchor_day, now=now)
        return expiry, anchor

    delta = plan.get_validity_timedelta()
    if delta is None:                       # UNLIMITED
        return None, None
    start = current if (current and current > now) else now
    return start + delta, None


def _resolve_payment_method(schema, method_id):
    from apps.billing.models.payment_models import InvoiceItemPayment

    if method_id:
        method = InvoiceItemPayment.objects.filter(pk=method_id, schema_name=schema).first()
        if not method:
            raise RenewalError("Payment method not found.")
        return method

    method = (
        InvoiceItemPayment.objects
        .filter(schema_name=schema, method_type='CASH')
        .order_by('-is_active', 'id')
        .first()
    )
    if method:
        return method

    # INACTIVE on purpose: an active CASH method could be picked as the STK gateway.
    return InvoiceItemPayment.objects.create(
        schema_name=schema,
        method_type='CASH',
        name='Cash / Manual',
        code=f"CASH_{secrets.token_hex(4).upper()}",
        is_active=False,
        minimum_amount=Decimal('1.00'),
        maximum_amount=Decimal('9999999.00'),
    )


def _dispatch_post_renewal(**kwargs):
    try:
        from apps.billing.tasks import post_renewal_actions
        post_renewal_actions.delay(**kwargs)
    except Exception:
        logger.warning("Could not queue post-renewal actions", exc_info=True)


def renew_subscription(
    customer,
    *,
    performed_by=None,
    record_payment: bool = True,
    amount=None,
    payment_method_id: Optional[int] = None,
    reference: str = '',
    notes: str = '',
    send_sms: Optional[bool] = None,
) -> dict:
    from apps.billing.models.billing_models import Invoice
    from apps.billing.models.payment_models import Payment
    from apps.billing.models.subscription_models import Subscription
    from apps.customers.models import Customer
    from apps.radius.models import CustomerRadiusCredentials, RadiusExpiryReminderLog
    from apps.radius.signals_auto_sync import _get_or_create_bandwidth_profile

    schema = connection.schema_name
    now = timezone.now()
    if send_sms is None:
        send_sms = record_payment

    lock_key = f"renew_lock:{schema}:{customer.pk}"
    if not cache.add(lock_key, "1", timeout=15):
        raise RenewalError("A renewal for this customer is already in progress.")

    try:
        pay_amount = None
        if record_payment:
            try:
                pay_amount = Decimal(str(amount)) if amount not in (None, '') else None
            except (InvalidOperation, ValueError):
                raise RenewalError("Invalid payment amount.")
            if pay_amount is not None and pay_amount <= 0:
                raise RenewalError("Payment amount must be greater than zero.")

        with transaction.atomic():
            creds = CustomerRadiusCredentials.objects.select_for_update().filter(customer_id=customer.pk).first()
            if not creds:
                raise RenewalError("This customer has no RADIUS credentials. Activate a service first.")

            service = _pick_service(customer.pk)
            if not service:
                raise RenewalError("No active service with a plan found. Assign a plan first.")
            plan = service.plan

            previous_expiry = creds.expiration_date
            was_disabled = (not creds.is_enabled) or bool(previous_expiry and previous_expiry <= now)
            new_expiry, new_anchor = _compute_expiry(plan, creds, now)

            # ── RADIUS credentials: one explicit sync; failure rolls everything back ──
            creds.expiration_date = new_expiry
            creds.is_enabled = True
            creds.disabled_reason = ''
            creds.subscription_activated_at = now          # ← resets usage counter
            if new_anchor:
                creds.billing_anchor_day = new_anchor
            profile = _get_or_create_bandwidth_profile(service)
            if profile and creds.bandwidth_profile_id != profile.id:
                creds.bandwidth_profile = profile

            creds._is_syncing = True                       # suppress duplicate signal sync
            try:
                creds.save()
                creds.sync_to_radius()
            finally:
                creds._is_syncing = False

            # ── Service / customer state (queryset update avoids stale-cache signal overwrites) ──
            if service.status != 'ACTIVE':
                service.status = 'ACTIVE'
                service.suspension_date = None
                if not service.activation_date:
                    service.activation_date = now
                service.save(update_fields=['status', 'suspension_date', 'activation_date'])

            Customer.objects.filter(
                pk=customer.pk, status__in=('SUSPENDED', 'INACTIVE', 'PENDING')
            ).update(status='ACTIVE')

            # ── Payment (optional) ──
            payment = None
            final_amount = pay_amount if pay_amount is not None else Decimal(str(plan.base_price or 0))
            if record_payment and final_amount > 0:
                method = _resolve_payment_method(schema, payment_method_id)
                invoice = (
                    Invoice.objects
                    .filter(customer_id=customer.pk,
                            status__in=['ISSUED', 'SENT', 'OVERDUE', 'PARTIAL'],
                            balance__gt=0)
                    .order_by('due_date', 'id')
                    .first()
                )
                actor = getattr(performed_by, 'get_full_name', lambda: '')() or getattr(performed_by, 'email', 'admin')
                payment = Payment.objects.create(
                    customer_id=customer.pk,               # id, not object → fresh customer in signals
                    invoice=invoice,
                    amount=final_amount,
                    payment_method=method,
                    status='COMPLETED',
                    payment_reference=reference or 'MANUAL-RENEW',
                    payment_date=now,
                    processed_at=now,
                    processed_by=performed_by,
                    is_reconciled=True,
                    reconciled_at=now,
                    payer_name=customer.full_name,
                    payer_phone=getattr(customer.user, 'phone_number', '') or '',
                    notes=notes or f"Manual subscription renewal by {actor}",
                    created_by=performed_by,
                    schema_name=schema,
                    service_type='PPPOE',
                )

            # ── Subscription history ──
            Subscription.objects.filter(customer_id=customer.pk, status='ACTIVE').update(status='EXPIRED')
            Subscription.objects.create(
                customer_id=customer.pk,
                service_connection=service,
                plan=plan,
                payment=payment,
                amount_paid=payment.amount if payment else Decimal('0'),
                status='ACTIVE',
                started_at=now,
                expires_at=new_expiry,
                schema_name=schema,
            )

            # ── Fresh reminder cycle ──
            RadiusExpiryReminderLog.objects.filter(customer_id=str(customer.pk)).delete()

            # ── After commit: CoA (only if needed) + SMS, off the request path ──
            nas_ip = None
            if was_disabled and creds.router_id:
                router = creds.router
                nas_ip = router.vpn_ip_address or router.ip_address

            post = dict(
                tenant_schema=schema,
                customer_id=customer.pk,
                plan_name=plan.name or '',
                expires_at_iso=new_expiry.isoformat() if new_expiry else None,
                reference=(payment.payment_reference if payment else ''),
                send_sms=bool(send_sms),
                kick_username=creds.username if nas_ip else None,
                nas_ip=nas_ip,
            )
            transaction.on_commit(lambda: _dispatch_post_renewal(**post))

        return {
            'username': creds.username,
            'plan_name': plan.name,
            'previous_expiration': previous_expiry.isoformat() if previous_expiry else None,
            'new_expiration': new_expiry.isoformat() if new_expiry else None,
            'payment': (
                {'id': payment.id, 'payment_number': payment.payment_number, 'amount': float(payment.amount)}
                if payment else None
            ),
        }
    finally:
        cache.delete(lock_key)