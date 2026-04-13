"""
Billing Celery Tasks — Cloud Controller Hotspot Tasks + Email Notifications

Periodic tasks for:
- Cleaning up expired hotspot sessions + RADIUS entries
- Expiring stale pending payments
- Sending billing reminder emails to customers
- Sending payment confirmation emails

NOTE: billing models live in TENANT_APPS, so every query must
run inside the correct tenant schema.  We iterate over all tenants
using django_tenants' schema_context.
"""

import logging
from celery import shared_task
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)


def _for_each_tenant(callback):
    """
    Run *callback(tenant)* inside every active tenant's schema.
    Returns aggregated results dict.
    """
    from django_tenants.utils import schema_context
    from apps.core.models import Tenant

    totals = {}
    for tenant in Tenant.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                result = callback(tenant)
                for k, v in (result or {}).items():
                    totals[k] = totals.get(k, 0) + (v if isinstance(v, (int, float)) else 0)
        except Exception as e:
            logger.error(
                f"Billing task error in tenant '{tenant.schema_name}': {e}",
                exc_info=True,
            )
    return totals


@shared_task(name='apps.billing.tasks.cleanup_expired_hotspot_sessions')
def cleanup_expired_hotspot_sessions():
    """
    Periodic task: Clean up expired hotspot sessions.
    
    1. Finds active sessions past their expiry time
    2. Revokes RADIUS credentials (dual-write: tenant + public schema)
    3. Marks sessions as expired in the database
    
    Runs every 5 minutes via Celery Beat.
    """
    def _cleanup(tenant):
        from apps.billing.services.hotspot_radius_service import HotspotRadiusService
        service = HotspotRadiusService()
        count = service.cleanup_expired_sessions()
        if count:
            logger.info(f"[{tenant.schema_name}] Cleaned up {count} expired hotspot sessions")
        return {'cleaned': count}

    try:
        return _for_each_tenant(_cleanup)
    except Exception as e:
        logger.error(f"Hotspot cleanup task failed: {e}", exc_info=True)
        return {'error': str(e)}


@shared_task(name='apps.billing.tasks.expire_stale_pending_payments')
def expire_stale_pending_payments():
    """
    Periodic task: Expire hotspot sessions stuck in 'pending' status.
    
    If a PayHero STK push was sent but never confirmed (user didn't enter PIN),
    the session stays pending forever. This task cleans them up after 10 minutes.
    
    Runs every 10 minutes via Celery Beat.
    """
    def _expire(tenant):
        from apps.billing.models.hotspot_models import HotspotSession

        cutoff = timezone.now() - timezone.timedelta(minutes=10)
        stale_sessions = HotspotSession.objects.filter(
            status='pending',
            created_at__lt=cutoff
        )
        count = stale_sessions.count()
        for session in stale_sessions:
            session.mark_failed('Payment timeout — STK push not confirmed')
        if count:
            logger.info(f"[{tenant.schema_name}] Expired {count} stale pending hotspot payments")
        return {'expired': count}

    try:
        return _for_each_tenant(_expire)
    except Exception as e:
        logger.error(f"Stale payment cleanup task failed: {e}", exc_info=True)
        return {'error': str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# EMAIL NOTIFICATIONS — Billing reminders & payment confirmations
# ═══════════════════════════════════════════════════════════════════════════

def _send_customer_email(customer, template, subject, context):
    """
    Send an email to a customer. Tries Resend first, falls back to Gmail SMTP.
    """
    from django.conf import settings as django_settings
    from django.template.loader import render_to_string
    from django.utils.html import strip_tags
    from django.core.mail import EmailMultiAlternatives

    email = getattr(customer, 'email', None)
    if not email and hasattr(customer, 'user'):
        email = customer.user.email
    if not email:
        return

    html_body = render_to_string(template, context)
    text_body = strip_tags(html_body)
    from_email = getattr(django_settings, 'DEFAULT_FROM_EMAIL', 'billing@netily.co.ke')

    # Send via Gmail SMTP
    try:
        msg = EmailMultiAlternatives(subject, text_body, from_email, [email])
        msg.attach_alternative(html_body, 'text/html')
        msg.send(fail_silently=False)
        logger.info(f"Sent billing email to {email}: {subject}")
    except Exception as e:
        logger.error(f"Failed to send billing email to {email}: {e}")


@shared_task(name='apps.billing.tasks.send_billing_reminder_emails')
def send_billing_reminder_emails():
    """
    Periodic task: Send payment reminder emails to customers with outstanding invoices.
    Runs daily. Sends reminders for unpaid invoices that are issued or overdue.
    """
    def _send_reminders(tenant):
        from apps.billing.models.billing_models import Invoice

        now = timezone.now()
        reminders_sent = 0

        # Find unpaid ISSUED invoices
        unpaid = Invoice.objects.filter(
            status__in=['ISSUED', 'OVERDUE'],
            customer__isnull=False,
        ).select_related('customer__user')

        for invoice in unpaid:
            customer = invoice.customer
            if not customer:
                continue

            days_overdue = 0
            if invoice.due_date and invoice.due_date < now.date():
                days_overdue = (now.date() - invoice.due_date).days

            company_name = ''
            try:
                company_name = tenant.company.name
            except Exception:
                company_name = tenant.schema_name

            customer_name = ''
            try:
                customer_name = customer.full_name or customer.user.get_full_name()
            except Exception:
                customer_name = 'Customer'

            context = {
                'customer_name': customer_name,
                'company_name': company_name,
                'invoice_number': invoice.invoice_number or str(invoice.id),
                'amount_due': f"{invoice.balance:,.2f}",
                'due_date': invoice.due_date,
                'days_overdue': days_overdue,
                'plan_name': '',
                'payment_link': '',
            }

            try:
                _send_customer_email(
                    customer=customer,
                    template='emails/billing/payment_reminder.html',
                    subject=f'Payment Reminder: Invoice #{invoice.invoice_number or invoice.id}',
                    context=context,
                )
                reminders_sent += 1
            except Exception as e:
                logger.error(f"Failed to send reminder for invoice {invoice.id}: {e}")

        if reminders_sent:
            logger.info(f"[{tenant.schema_name}] Sent {reminders_sent} billing reminder emails")
        return {'reminders_sent': reminders_sent}

    try:
        return _for_each_tenant(_send_reminders)
    except Exception as e:
        logger.error(f"Billing reminder email task failed: {e}", exc_info=True)
        return {'error': str(e)}


@shared_task(name='apps.billing.tasks.send_payment_confirmation_email')
def send_payment_confirmation_email(customer_id, amount, reference='', payment_method='', tenant_schema=None):
    """
    One-off task: Send payment confirmation email to a specific customer.
    Called from the payment signal when a payment is completed.
    """
    from django_tenants.utils import schema_context

    if not tenant_schema:
        from django.db import connection
        tenant_schema = connection.schema_name

    with schema_context(tenant_schema):
        try:
            from apps.customers.models import Customer
            customer = Customer.objects.select_related('user').get(id=customer_id)

            company_name = ''
            try:
                from apps.core.models import Tenant
                t = Tenant.objects.filter(schema_name=tenant_schema).first()
                if t:
                    company_name = t.company.name
            except Exception:
                pass

            customer_name = ''
            try:
                customer_name = customer.full_name or customer.user.get_full_name()
            except Exception:
                customer_name = 'Customer'

            context = {
                'customer_name': customer_name,
                'company_name': company_name,
                'amount_paid': f"{amount:,.2f}",
                'reference': reference,
                'payment_method': payment_method,
                'payment_date': timezone.now(),
                'remaining_balance': float(customer.outstanding_balance or 0),
            }

            _send_customer_email(
                customer=customer,
                template='emails/billing/payment_confirmation.html',
                subject=f'Payment Received: KES {amount:,.2f}',
                context=context,
            )
        except Exception as e:
            logger.error(f"Payment confirmation email failed for customer {customer_id}: {e}")
