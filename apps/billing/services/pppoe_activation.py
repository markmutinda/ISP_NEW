import logging
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def activate_pppoe_from_payment(payment):
    """
    Idempotent PPPoE renewal for a COMPLETED payment.
    Single code path for STK (Daraja), Netily webhook and C2B-matched portal payments.
    Returns the new expiry (or None for unlimited / nothing to do).
    """
    from django.db import connection
    from apps.customers.models import Customer, ServiceConnection
    from apps.billing.models.payment_models import Payment
    from apps.billing.models.subscription_models import Subscription
    from apps.billing.tasks import post_renewal_actions
    from apps.radius.models import CustomerRadiusCredentials, RadiusExpiryReminderLog

    if not payment.customer_id:
        return None

    schema = connection.schema_name
    now = timezone.now()
    marker = f"[renewed:{payment.pk}]"

    with transaction.atomic():
        fresh = Payment.objects.select_for_update().only("id", "notes").get(pk=payment.pk)
        if marker in (fresh.notes or ""):
            return None  # already applied

        creds = (CustomerRadiusCredentials.objects.select_for_update()
                 .select_related("router").filter(customer_id=payment.customer_id).first())
        service = (ServiceConnection.objects
                   .filter(customer_id=payment.customer_id,
                           status__in=["ACTIVE", "SUSPENDED"], plan__isnull=False)
                   .select_related("plan").order_by("-created_at").first())
        if not creds or not service:
            logger.warning("PPPoE activation skipped for payment %s: no creds/service", payment.pk)
            return None

        plan = service.plan
        current = creds.expiration_date
        if (plan.validity_type or "DAYS").upper() == "CALENDAR_MONTH":
            from utils.billing_dates import resolve_calendar_renewal
            anchor, new_expiry = resolve_calendar_renewal(current, anchor_day=creds.billing_anchor_day, now=now)
            creds.billing_anchor_day = anchor
        else:
            delta = plan.get_validity_timedelta()
            new_expiry = None if delta is None else (current if current and current > now else now) + delta

        was_disabled = (not creds.is_enabled) or bool(current and current <= now)

        creds.expiration_date = new_expiry
        creds.is_enabled = True
        creds.disabled_reason = ""
        creds.subscription_activated_at = now
        creds._is_syncing = True          # suppress duplicate signal sync
        try:
            creds.save()
            creds.sync_to_radius()
        finally:
            creds._is_syncing = False

        if service.status != "ACTIVE":
            ServiceConnection.objects.filter(pk=service.pk).update(status="ACTIVE")
        Customer.objects.filter(
            pk=payment.customer_id, status__in=("SUSPENDED", "INACTIVE", "PENDING")
        ).update(status="ACTIVE")

        Subscription.objects.filter(customer_id=payment.customer_id, status="ACTIVE").update(status="EXPIRED")
        Subscription.objects.create(
            customer_id=payment.customer_id, service_connection=service, plan=plan,
            payment=payment, amount_paid=payment.amount, status="ACTIVE",
            started_at=now, expires_at=new_expiry, schema_name=schema,
        )
        RadiusExpiryReminderLog.objects.filter(customer_id=str(payment.customer_id)).delete()
        Payment.objects.filter(pk=payment.pk).update(notes=f"{fresh.notes or ''} {marker}".strip())

        nas_ip = None
        if creds.router_id:
            nas_ip = creds.router.vpn_ip_address or creds.router.ip_address
        post = dict(
            tenant_schema=schema, customer_id=payment.customer_id, plan_name=plan.name or "",
            expires_at_iso=new_expiry.isoformat() if new_expiry else None,
            reference=payment.mpesa_receipt or payment.payment_reference or "",
            send_sms=True,
            kick_username=creds.username if (nas_ip and was_disabled) else None,
            nas_ip=nas_ip,
        )
        transaction.on_commit(lambda: post_renewal_actions.delay(**post))

    return new_expiry