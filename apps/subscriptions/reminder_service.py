"""
Subscription payment reminder SMS — uses Netily's own Bytewave master
account (not tenant gateways) to nudge each tenant's admin phone before
their platform subscription is due.
"""
import logging
from decimal import Decimal
from django.conf import settings
from django.utils import timezone
from django_tenants.utils import schema_context, get_public_schema_name

logger = logging.getLogger(__name__)

REMINDER_MILESTONES = [('3_day', 3), ('1_day', 1)]

TEMPLATE_VARIABLES = [
    {'key': '{company_name}', 'label': 'Company Name', 'example': 'Bluenet ISP'},
    {'key': '{plan_name}', 'label': 'Plan Name', 'example': 'Metered'},
    {'key': '{days_left}', 'label': 'Days Left', 'example': '3'},
    {'key': '{expiry_date}', 'label': 'Expiry Date', 'example': '12 Sep 2026'},
    {'key': '{amount_due}', 'label': 'Amount Due (KES)', 'example': '2,500'},
    {'key': '{admin_name}', 'label': 'Admin First Name', 'example': 'Jane'},
    {'key': '{invoice_number}', 'label': 'Invoice Number', 'example': 'NET-BILL-260912090000'},
]


def _master_backend():
    from apps.messaging.services.gateway_dispatcher import BytewaveBackend
    return BytewaveBackend(
        api_key=settings.BYTEWAVE_API_TOKEN,
        sender_id=settings.BYTEWAVE_SENDER_ID,
        extra_config={'base_url': settings.BYTEWAVE_BASE_URL},
    )


def _fmt_phone(phone: str) -> str:
    p = ''.join(filter(str.isdigit, str(phone or '')))
    if p.startswith('0'):
        p = '254' + p[1:]
    elif p.startswith('7') or p.startswith('1'):
        p = '254' + p
    elif not p.startswith('254'):
        p = '254' + p
    return f"+{p}"


def get_tenant_admin_phone(tenant):
    """The tenant's original admin (created at signup) is the SMS target."""
    with schema_context(tenant.schema_name):
        from apps.core.models import User
        admin = (
            User.objects.filter(role='admin', is_active=True)
            .exclude(phone_number='')
            .order_by('date_joined')
            .first()
        )
        return (admin.phone_number, admin.first_name) if admin else (None, '')


def render_reminder_message(subscription, days_left, expiry_dt):
    from .models import SubscriptionReminderTemplate

    template = SubscriptionReminderTemplate.get_active()
    tenant = getattr(subscription.company, 'tenant', None)
    admin_phone, admin_name = (None, '')
    if tenant:
        admin_phone, admin_name = get_tenant_admin_phone(tenant)

    amount = (
        subscription.plan.base_license_fee
        if subscription.plan and subscription.plan.is_metered
        else subscription.current_price
    ) if subscription.plan else Decimal('0')

    ctx = {
        'company_name': subscription.company.name,
        'plan_name': subscription.plan.name if subscription.plan else '',
        'days_left': str(days_left),
        'expiry_date': timezone.localtime(expiry_dt).strftime('%d %b %Y'),
        'amount_due': f"{float(amount):,.0f}",
        'admin_name': admin_name or 'there',
    }
    msg = template.content
    for key, value in ctx.items():
        msg = msg.replace('{' + key + '}', str(value))
    return msg, admin_phone


def send_subscription_expiry_reminders():
    """
    Daily sweep: sends SMS 3 days and 1 day before a tenant's platform
    subscription payment is due. Idempotent per (subscription, milestone,
    period_end) via SubscriptionReminderLog.
    """
    from .models import CompanySubscription, SubscriptionReminderLog

    now = timezone.now()
    sent = skipped = failed = 0

    with schema_context(get_public_schema_name()):
        subs = (
            CompanySubscription.objects
            .filter(status='active', is_trial=False)
            .select_related('company', 'plan', 'company__tenant')
        )

        for sub in subs:
            if not sub.current_period_end:
                continue
            days_left = (sub.current_period_end.date() - now.date()).days
            milestone = next((name for name, t in REMINDER_MILESTONES if days_left == t), None)
            if not milestone:
                continue

            if SubscriptionReminderLog.objects.filter(
                subscription=sub, milestone=milestone, period_end=sub.current_period_end
            ).exists():
                skipped += 1
                continue

            try:
                message, phone = render_reminder_message(sub, days_left, sub.current_period_end)
                if not phone:
                    SubscriptionReminderLog.objects.create(
                        subscription=sub, milestone=milestone, period_end=sub.current_period_end,
                        status='failed', error='No tenant admin phone number on file',
                    )
                    failed += 1
                    continue

                ok, _provider_id, _cost = _master_backend().send(_fmt_phone(phone), message)
                SubscriptionReminderLog.objects.create(
                    subscription=sub, milestone=milestone, period_end=sub.current_period_end,
                    phone_number=phone, status='sent' if ok else 'failed',
                    error='' if ok else 'Provider reported failure',
                )
                sent += 1 if ok else 0
                failed += 0 if ok else 1
            except Exception as exc:
                logger.exception("Subscription reminder failed for company %s", sub.company_id)
                SubscriptionReminderLog.objects.create(
                    subscription=sub, milestone=milestone, period_end=sub.current_period_end,
                    status='failed', error=str(exc)[:500],
                )
                failed += 1

    logger.info("Subscription reminders: sent=%s skipped=%s failed=%s", sent, skipped, failed)
    return {'sent': sent, 'skipped': skipped, 'failed': failed}
