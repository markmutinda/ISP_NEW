"""
Billing Celery Tasks — Cloud Controller Hotspot Tasks + Email Notifications

Periodic tasks for:
- Cleaning up expired hotspot sessions + RADIUS entries
- Expiring stale pending payments
- Sending billing reminder emails to customers
- Sending payment confirmation emails
- Sending hotspot expiry warnings
- Notifying expired hotspot sessions
- Auto-generating invoices for PPPoE subscribers
- Pruning stale hotspot clients

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

        cutoff = timezone.now() - timedelta(minutes=10)
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
# HOTSPOT EXPIRY WARNINGS (SMS) — TEMPORARILY DISABLED
# ═══════════════════════════════════════════════════════════════════════════

@shared_task(name='apps.billing.tasks.send_hotspot_expiry_warnings')
def send_hotspot_expiry_warnings():
    """
    Send expiry warning SMS to hotspot users whose sessions are about
    to expire. (Temporarily disabled due to missing model fields).
    """
    def _warn(tenant):
        # Hotspot expiry minutes logic removed/disabled
        return {'warned': 0}

    try:
        return _for_each_tenant(_warn)
    except Exception as e:
        logger.error(f"Hotspot expiry warning task failed: {e}", exc_info=True)
        return {'error': str(e)}


@shared_task(name='apps.billing.tasks.notify_expired_hotspot_sessions')
def notify_expired_hotspot_sessions():
    """
    Send 'session expired' SMS after marking sessions expired.
    Runs every 5 minutes (chained with cleanup task).
    """
    def _notify(tenant):
        from apps.billing.models.hotspot_models import HotspotSession
        from apps.messaging.services.notification_sender import SMSNotifier

        # Find sessions that just expired (within last 10 min) and haven't been notified
        now = timezone.now()
        recently_expired = HotspotSession.objects.filter(
            status='expired',
            expires_at__gte=now - timedelta(minutes=10),
            expires_at__lte=now,
        )
        count = 0
        for session in recently_expired:
            try:
                SMSNotifier.hotspot_session_expired(session)
                count += 1
            except Exception as e:
                logger.warning(f"Expired SMS failed for {session.session_id}: {e}")
        return {'notified': count}

    try:
        return _for_each_tenant(_notify)
    except Exception as e:
        logger.error(f"Notify expired sessions task failed: {e}", exc_info=True)
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


# ═══════════════════════════════════════════════════════════════════════════
# PPPOE AUTO-INVOICE GENERATION (NEW)
# ═══════════════════════════════════════════════════════════════════════════

@shared_task(name='apps.billing.tasks.auto_generate_pppoe_invoices')
def auto_generate_pppoe_invoices():
    """
    Auto-generate invoices for PPPoE subscribers whose subscriptions
    are expiring soon or have expired. Respects per-tenant toggle.
    
    Runs daily via Celery Beat at 6:30 AM.
    """
    def _generate(tenant):
        from apps.billing.models.billing_models import InvoiceSettings, Invoice, InvoiceItem, Plan
        from apps.radius.models import CustomerRadiusCredentials
        from decimal import Decimal

        settings = InvoiceSettings.get_settings(tenant.schema_name)
        if not settings.auto_generate_enabled:
            return {'generated': 0, 'skipped_disabled': 1}

        now = timezone.now()
        threshold = now + timedelta(days=settings.days_before_expiry)
        generated = 0
        skipped_no_plan = 0
        skipped_no_price = 0
        skipped_existing = 0

        # Find credentials expiring within threshold or already expired (not yet invoiced)
        expiring_creds = CustomerRadiusCredentials.objects.filter(
            expiration_date__isnull=False,
            expiration_date__lte=threshold,
            is_enabled=True,
        ).select_related('customer__user', 'bandwidth_profile')

        for cred in expiring_creds:
            customer = cred.customer
            if not customer:
                continue

            # Check if an unpaid invoice already exists for this customer
            existing = Invoice.objects.filter(
                customer=customer,
                status__in=['DRAFT', 'ISSUED', 'SENT', 'PARTIAL', 'OVERDUE'],
            ).exists()

            if existing:
                skipped_existing += 1
                continue

            # Get active service and plan
            service = customer.services.filter(
                status='ACTIVE',
                plan__isnull=False,
            ).first()

            if not service or not service.plan:
                skipped_no_plan += 1
                continue

            plan = service.plan
            amount = plan.base_price or Decimal('0')
            if amount <= 0:
                skipped_no_price += 1
                continue

            due_date = (cred.expiration_date or now).date()

            try:
                invoice = Invoice.objects.create(
                    customer=customer,
                    billing_date=now.date(),
                    due_date=due_date,
                    status='ISSUED',
                    service_connection=service,
                    plan=plan,
                    notes=f'Auto-generated for {plan.name} subscription renewal',
                    service_period_start=now.date(),
                    service_period_end=due_date,
                )

                InvoiceItem.objects.create(
                    invoice=invoice,
                    description=f'{plan.name} - Internet Subscription',
                    quantity=1,
                    unit_price=amount,
                    tax_rate=Decimal('0'),
                    tax_amount=Decimal('0'),
                    total=amount,
                    service_type='INTERNET',
                    service_period_start=now.date(),
                    service_period_end=due_date,
                )

                # Recalculate totals
                invoice.subtotal = amount
                invoice.tax_amount = Decimal('0')
                invoice.total_amount = amount
                invoice.balance = amount
                invoice.save()

                generated += 1
                logger.info(f"[{tenant.schema_name}] Auto-generated invoice {invoice.invoice_number} for {customer.customer_code}")
            except Exception as e:
                logger.error(f"[{tenant.schema_name}] Failed to generate invoice for {customer.customer_code}: {e}")

        return {
            'generated': generated,
            'skipped_disabled': 0,
            'skipped_no_plan': skipped_no_plan,
            'skipped_no_price': skipped_no_price,
            'skipped_existing': skipped_existing,
        }

    try:
        return _for_each_tenant(_generate)
    except Exception as e:
        logger.error(f"Auto invoice generation task failed: {e}", exc_info=True)
        return {'error': str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# HOTSPOT CLIENT PRUNING TASK
# ═══════════════════════════════════════════════════════════════════════════

@shared_task(name='apps.billing.tasks.prune_stale_hotspot_clients')
def prune_stale_hotspot_clients():
    """
    Deletes hotspot clients (cascading to their sessions + devices) and revokes
    RADIUS credentials for clients whose most recent session expired more than
    the tenant's configured prune window ago. Respects per-tenant
    HotspotPruneSettings (default 30 days).
    """
    def _prune(tenant):
        from apps.billing.models.hotspot_models import (
            HotspotPruneSettings, HotspotClient, HotspotSession,
        )
        from apps.billing.services.hotspot_radius_service import HotspotRadiusService
        from apps.radius.services.radius_sync_service import RadiusSyncService

        settings_obj = HotspotPruneSettings.get_settings(tenant.schema_name)
        if not settings_obj.is_enabled:
            return {'pruned': 0, 'skipped_disabled': 1}

        cutoff = timezone.now() - timedelta(days=settings_obj.prune_window_days)
        radius_service = HotspotRadiusService()
        sync_service = RadiusSyncService()
        pruned = 0

        for client in HotspotClient.objects.filter(schema_name=tenant.schema_name).iterator():
            # Never touch a client with a currently active/paid session
            still_active = HotspotSession.objects.filter(
                hotspot_client=client,
                status__in=('active', 'paid'),
                expires_at__gt=timezone.now(),
            ).exists()
            if still_active:
                continue

            last_session = HotspotSession.objects.filter(
                hotspot_client=client
            ).order_by('-expires_at').first()

            reference_time = (
                last_session.expires_at
                if last_session and last_session.expires_at
                else client.last_seen_at
            )
            if not reference_time or reference_time >= cutoff:
                continue

            access_codes = (
                HotspotSession.objects.filter(hotspot_client=client)
                .exclude(access_code__isnull=True)
                .values_list('access_code', flat=True)
                .distinct()
            )
            for code in access_codes:
                try:
                    radius_service.revoke_credentials(code)
                    sync_service.delete_radius_user(code)
                except Exception as e:
                    logger.warning(f"[{tenant.schema_name}] RADIUS cleanup failed for {code}: {e}")

            client.delete()  # cascades to HotspotSession + HotspotClientDevice
            pruned += 1

        settings_obj.last_pruned_at = timezone.now()
        settings_obj.save(update_fields=['last_pruned_at'])

        if pruned:
            logger.info(
                f"[{tenant.schema_name}] Pruned {pruned} stale hotspot clients "
                f"(window={settings_obj.prune_window_days}d)"
            )
        return {'pruned': pruned}

    try:
        return _for_each_tenant(_prune)
    except Exception as e:
        logger.error(f"Hotspot client pruning task failed: {e}", exc_info=True)
        return {'error': str(e)}