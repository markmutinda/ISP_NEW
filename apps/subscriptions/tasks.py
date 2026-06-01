# apps/subscriptions/tasks.py
import logging
from itertools import islice
from datetime import timedelta
from decimal import Decimal
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Sum
from django.contrib.auth import get_user_model
from django_tenants.utils import schema_context, get_public_schema_name

from apps.subscriptions.models import BillingCycle, BillableClientRecord
from apps.billing.models import Invoice, InvoiceItem
from apps.customers.models import Customer
# Import RadAcct for True Usage sweeping
from apps.radius.models import RadAcct

logger = logging.getLogger(__name__)
User = get_user_model()

def batched(iterable, n):
    it = iter(iterable)
    while batch := tuple(islice(it, n)):
        yield batch

@shared_task
def generate_metered_invoices():
    now = timezone.now()
    
    # 1. Find all active cycles that have ended
    ended_cycles = BillingCycle.objects.filter(
        status='active',
        end_date__lte=now
    ).exclude(
        subscription__status='trialing'
    ).select_related('tenant', 'subscription', 'subscription__plan')
    
    if not ended_cycles.exists():
        return "No cycles to process"

    for cycle in ended_cycles:
        tenant = cycle.tenant
        plan = cycle.subscription.plan
        new_invoice = None
        
        try:
            with transaction.atomic():
                
                # ─── PHASE 2: SWEEP TRUE USAGE FROM ROUTER LOGS ───
                # FIX: Added framedprotocol='PPP' discriminator to guarantee we only bill for PPPoE sessions
                # This prevents Hotspot logins from being incorrectly counted as PPPoE users.
                # Standard MikroTik PPPoE sessions log Framed-Protocol = PPP.
                with schema_context(tenant.schema_name):
                    # Find all unique usernames that had an active PPPoE session during this specific cycle
                    active_usernames = list(RadAcct.objects.filter(
                        acctstarttime__lt=cycle.end_date,
                        framedprotocol='PPP'  # CRITICAL: Only count PPPoE sessions, not Hotspot
                    ).filter(
                        Q(acctstoptime__isnull=True) | Q(acctstoptime__gt=cycle.start_date)
                    ).values_list('username', flat=True).distinct())
                    
                    logger.info(f"[{tenant.name}] Found {len(active_usernames)} unique PPPoE users with active sessions during cycle")

                    # ─── ACTUAL HOTSPOT REVENUE FROM DATABASE ───
                    # Query the REAL paid sessions instead of trusting the accumulator.
                    # This is the single source of truth for hotspot billing.
                    from apps.billing.models.hotspot_models import HotspotSession
                    actual_hotspot_revenue = HotspotSession.objects.filter(
                        status__in=['active', 'expired'],
                        activated_at__gte=cycle.start_date,
                        activated_at__lt=cycle.end_date,
                    ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

                    hotspot_session_count = HotspotSession.objects.filter(
                        status__in=['active', 'expired'],
                        activated_at__gte=cycle.start_date,
                        activated_at__lt=cycle.end_date,
                    ).count()

                    logger.info(
                        f"[{tenant.name}] Actual hotspot revenue from {hotspot_session_count} "
                        f"paid sessions: KES {actual_hotspot_revenue:,.2f} "
                        f"(accumulator was: KES {cycle.hotspot_revenue_accumulated:,.2f})"
                    )

                # Switch to public schema to insert the ghost records for billing
                with schema_context(get_public_schema_name()):
                    records_to_create = [
                        BillableClientRecord(cycle=cycle, username=uname)
                        for uname in active_usernames if uname
                    ]
                    if records_to_create:
                        BillableClientRecord.objects.bulk_create(records_to_create, ignore_conflicts=True)
                        logger.info(f"[{tenant.name}] Created {len(records_to_create)} billable client records")
                
                    # ─── PHASE 3: THE EXACT MATH ───
                    base_fee = cycle.snapshot_base_fee
                    pppoe_count = cycle.calculate_total_pppoe()
                    pppoe_fee = cycle.calculate_pppoe_charge()
                    
                    # Use ACTUAL hotspot revenue queried from DB, not the accumulator
                    hotspot_share = (actual_hotspot_revenue * (cycle.snapshot_hotspot_share_pct / Decimal('100.0'))).quantize(Decimal('0.01'))
                    total_due = base_fee + pppoe_fee + hotspot_share
                    
                    logger.info(f"[{tenant.name}] Calculated charges: Base={base_fee}, PPPoE({pppoe_count})={pppoe_fee}, Hotspot={hotspot_share}, Total={total_due}")
                    
                    # ─── GRACE PERIOD: DO NOT LOCK IMMEDIATELY ───
                    # Set grace_ends_at to 4 days from now. The separate
                    # enforce_billing_grace_period task will lock the account
                    # only after the grace period expires.
                    grace_deadline = now + timedelta(days=4)
                    
                # ─── CREATE INVOICE IN TENANT SCHEMA ───
                with schema_context(tenant.schema_name):
                    # Ensure System User exists
                    user, _ = User.objects.get_or_create(
                        email='billing@netily.io',
                        defaults={'first_name': 'Netily', 'last_name': 'Platform'}
                    )
                    
                    # Ensure System Customer exists
                    sys_customer, _ = Customer.objects.get_or_create(
                        customer_code='NET-001',
                        defaults={'user': user, 'status': 'active'}
                    )

                    # Create Invoice
                    new_invoice = Invoice.objects.create(
                        invoice_number=f'NET-BILL-{now.strftime("%y%m%d%H%M%S")}',
                        customer=sys_customer,
                        total_amount=total_due,
                        status='ISSUED',
                        service_period_start=cycle.start_date.date(),
                        service_period_end=cycle.end_date.date(),
                        due_date=(now + timedelta(days=4)).date(),  # 4-day grace period
                        billing_date=now.date(),
                    )

                    # Create Exact Line Items (Breakdown)
                    InvoiceItem.objects.create(
                        invoice=new_invoice, 
                        description='Netily Platform - Base License Fee',
                        quantity=1, 
                        unit_price=base_fee, 
                        tax_rate=0, 
                        tax_amount=0, 
                        total=base_fee
                    )
                    if pppoe_fee > 0:
                        InvoiceItem.objects.create(
                            invoice=new_invoice, 
                            description=f'Active PPPoE Clients ({pppoe_count} users @ KES {cycle.snapshot_pppoe_price} each)',
                            quantity=pppoe_count,
                            unit_price=cycle.snapshot_pppoe_price, 
                            tax_rate=0, 
                            tax_amount=0, 
                            total=pppoe_fee
                        )
                    if hotspot_share > 0:
                        InvoiceItem.objects.create(
                            invoice=new_invoice, 
                            description=f'Hotspot Revenue Share ({cycle.snapshot_hotspot_share_pct}% of KES {actual_hotspot_revenue:,.0f})',
                            # ↑ Uses actual DB figure, not accumulator
                            quantity=1, 
                            unit_price=hotspot_share, 
                            tax_rate=0, 
                            tax_amount=0, 
                            total=hotspot_share
                        )

                with schema_context(get_public_schema_name()):
                    # Reconcile the accumulator with actual DB figures
                    if cycle.hotspot_revenue_accumulated != actual_hotspot_revenue:
                        logger.warning(
                            f"[{tenant.name}] Reconciling hotspot accumulator: "
                            f"KES {cycle.hotspot_revenue_accumulated} → KES {actual_hotspot_revenue}"
                        )
                        cycle.hotspot_revenue_accumulated = actual_hotspot_revenue

                    cycle.status = 'invoiced'
                    cycle.invoice_reference = str(new_invoice.id)
                    cycle.grace_ends_at = grace_deadline
                    cycle.save(update_fields=['status', 'invoice_reference', 'grace_ends_at', 'hotspot_revenue_accumulated'])
                    
                    logger.info(
                        f"[{tenant.name}] Invoiced KES {total_due} (Invoice #{new_invoice.id}). "
                        f"Billed {pppoe_count} PPPoE users (raw={cycle.get_raw_pppoe_count()}, min_floor={cycle.snapshot_min_clients}). "
                        f"Grace period ends: {grace_deadline.date()}"
                    )
                    
                    # ─── SEND INVOICE EMAIL ───
                    try:
                        _send_lifecycle_email(
                            tenant=tenant,
                            template='emails/billing/invoice_generated.html',
                            subject='Your Monthly Netily Invoice is Ready',
                            context={
                                'base_fee': base_fee,
                                'pppoe_count': pppoe_count,
                                'pppoe_fee': pppoe_fee,
                                'hotspot_share': hotspot_share,
                                'total_due': total_due,
                                'due_date': (now + timedelta(days=4)).date(),
                                'grace_days': 4,
                                'cycle_start': cycle.start_date.date(),
                                'cycle_end': cycle.end_date.date(),
                            }
                        )
                    except Exception as mail_err:
                        logger.warning(f"[{tenant.name}] Failed to send invoice email: {mail_err}")

        except Exception as e:
            logger.error(f"Failed to process billing cycle for tenant {tenant.name}: {str(e)}", exc_info=True)

    return f"Processed {ended_cycles.count()} billing cycles."


# ═══════════════════════════════════════════════════════════════════════════
# HELPER: Send lifecycle emails to tenant admin(s)
# ═══════════════════════════════════════════════════════════════════════════

def _send_lifecycle_email(tenant, template, subject, context):
    """
    Send an email to the tenant's admin user(s) using template rendering.
    Falls back to Resend if configured, otherwise uses Django SMTP.
    """
    from django.core.mail import EmailMultiAlternatives
    from django.template.loader import render_to_string
    from django.utils.html import strip_tags
    from django.conf import settings

    company = tenant.company
    context.update({
        'company_name': company.name,
        'tenant_subdomain': tenant.subdomain,
        'platform_url': getattr(settings, 'FRONTEND_URL', 'https://app.netily.co.ke'),
    })

    html_body = render_to_string(template, context)
    text_body = strip_tags(html_body)
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'billing@netily.co.ke')

    # Collect admin emails from the company
    admin_emails = []
    try:
        from django_tenants.utils import schema_context
        with schema_context(tenant.schema_name):
            admins = User.objects.filter(role__in=['admin', 'owner'], is_active=True)
            admin_emails = [u.email for u in admins if u.email]
    except Exception:
        pass

    # Fallback to company contact email
    if not admin_emails:
        contact = getattr(company, 'contact_email', None) or getattr(company, 'email', None)
        if contact:
            admin_emails = [contact]

    if not admin_emails:
        logger.warning(f"[{tenant.name}] No admin emails found, cannot send lifecycle email: {subject}")
        return

    # Send via Gmail SMTP
    msg = EmailMultiAlternatives(subject, text_body, from_email, admin_emails)
    msg.attach_alternative(html_body, 'text/html')
    msg.send(fail_silently=True)
    logger.info(f"[{tenant.name}] Sent lifecycle email via SMTP: {subject}")


# ═══════════════════════════════════════════════════════════════════════════
# TASK: Trial Lifecycle Checker
# Runs daily at 8 AM Nairobi time
# ═══════════════════════════════════════════════════════════════════════════

@shared_task
def check_trial_lifecycle():
    """
    Handles the full trial notification lifecycle:
    - Day 12: Send "trial ending in 48 hours" warning
    - Day 15 (trial_ends_at passed): Set status to expired, send lockout email
    """
    from apps.subscriptions.models import CompanySubscription
    now = timezone.now()

    # ── Warning: 48 hours before trial ends (Day 12) ──
    warning_window_start = now + timedelta(hours=46)
    warning_window_end = now + timedelta(hours=50)

    expiring_soon = CompanySubscription.objects.filter(
        status='trialing',
        is_trial=True,
        trial_ends_at__gte=warning_window_start,
        trial_ends_at__lte=warning_window_end,
    ).select_related('company')

    warned = 0
    for sub in expiring_soon:
        tenant = _get_tenant_for_company(sub.company)
        if tenant:
            try:
                _send_lifecycle_email(
                    tenant=tenant,
                    template='emails/billing/trial_warning.html',
                    subject='Your Free Trial Ends in 48 Hours',
                    context={
                        'trial_ends_at': sub.trial_ends_at,
                        'days_remaining': sub.trial_days_remaining,
                        'base_fee': sub.plan.base_license_fee,
                    }
                )
                warned += 1
            except Exception as e:
                logger.error(f"Failed to send trial warning to {sub.company.name}: {e}")

    # ── Lockout: Trial expired (Day 15+) ──
    expired_trials = CompanySubscription.objects.filter(
        status='trialing',
        is_trial=True,
        trial_ends_at__lt=now,
    ).select_related('company')

    locked = 0
    for sub in expired_trials:
        tenant = _get_tenant_for_company(sub.company)
        if tenant:
            try:
                # Explicitly set status to 'expired' so the frontend and middleware
                # both consistently see the lockout state.
                if sub.status != 'expired':
                    sub.status = 'expired'
                    sub.save(update_fields=['status'])
                    logger.info(f"[{sub.company.name}] Trial expired — marked status='expired'")

                _send_lifecycle_email(
                    tenant=tenant,
                    template='emails/billing/trial_expired.html',
                    subject='Action Required: Your Netily Trial Has Expired',
                    context={
                        'trial_duration_days': sub.TRIAL_DURATION_DAYS,
                        'base_fee': sub.plan.base_license_fee,
                    }
                )
                locked += 1
            except Exception as e:
                logger.error(f"Failed to send trial expired email to {sub.company.name}: {e}")

    return f"Trial lifecycle: {warned} warned, {locked} expired notifications sent."


# ═══════════════════════════════════════════════════════════════════════════
# TASK: Grace Period Enforcement
# Runs daily at 12:15 AM Nairobi time (after invoice generation at 12:05)
# ═══════════════════════════════════════════════════════════════════════════

@shared_task
def enforce_billing_grace_period():
    """
    Handles the billing grace period enforcement:
    - Day 33 (1 day before grace expires): Send urgent warning
    - Day 34 (grace_ends_at passed): Lock the account (past_due)
    """
    from apps.subscriptions.models import BillingCycle
    now = timezone.now()

    # ── Day 33 Warning: Grace expires in ~24 hours ──
    warn_start = now + timedelta(hours=22)
    warn_end = now + timedelta(hours=26)

    warning_cycles = BillingCycle.objects.filter(
        status='invoiced',
        grace_ends_at__gte=warn_start,
        grace_ends_at__lte=warn_end,
    ).select_related('tenant', 'subscription', 'subscription__company')

    warned = 0
    for cycle in warning_cycles:
        try:
            _send_lifecycle_email(
                tenant=cycle.tenant,
                template='emails/billing/grace_warning.html',
                subject='URGENT: 24 Hours Until Network Suspension',
                context={
                    'grace_ends_at': cycle.grace_ends_at,
                    'total_due': cycle.calculate_total_charge(),
                    'invoice_ref': cycle.invoice_reference,
                }
            )
            warned += 1
        except Exception as e:
            logger.error(f"Failed to send grace warning to {cycle.tenant.name}: {e}")

    # ── Day 34 Lockout: Grace has expired ──
    expired_cycles = BillingCycle.objects.filter(
        status='invoiced',
        grace_ends_at__lt=now,
    ).select_related('tenant', 'subscription', 'subscription__company')

    locked = 0
    for cycle in expired_cycles:
        try:
            with transaction.atomic():
                sub = cycle.subscription
                sub.status = 'past_due'
                sub.save(update_fields=['status'])

                logger.info(
                    f"[{cycle.tenant.name}] Grace period expired. "
                    f"Subscription locked (past_due). Invoice: {cycle.invoice_reference}"
                )

                _send_lifecycle_email(
                    tenant=cycle.tenant,
                    template='emails/billing/suspension_notice.html',
                    subject='Notice of Network Suspension',
                    context={
                        'total_due': cycle.calculate_total_charge(),
                        'invoice_ref': cycle.invoice_reference,
                    }
                )
                locked += 1
        except Exception as e:
            logger.error(f"Failed to enforce grace period for {cycle.tenant.name}: {e}")

    return f"Grace enforcement: {warned} warned, {locked} locked."


# ═══════════════════════════════════════════════════════════════════════════
# TASK: Mid-Cycle PPPoE Ghost Record Sweep
# Runs daily at 12:10 AM — captures PPPoE users connecting mid-cycle
# ═══════════════════════════════════════════════════════════════════════════

@shared_task
def sweep_pppoe_ghost_records():
    """
    Daily sweep of RadAcct for PPPoE sessions during active billing cycles.
    Creates BillableClientRecord entries so the invoice total is always
    up-to-date even before the cycle ends.
    """
    from apps.subscriptions.models import BillingCycle, BillableClientRecord
    now = timezone.now()

    active_cycles = BillingCycle.objects.filter(
        status='active',
    ).select_related('tenant')

    total_created = 0
    for cycle in active_cycles:
        try:
            with schema_context(cycle.tenant.schema_name):
                active_usernames = list(RadAcct.objects.filter(
                    acctstarttime__lt=now,
                    framedprotocol='PPP',
                ).filter(
                    Q(acctstoptime__isnull=True) | Q(acctstoptime__gt=cycle.start_date)
                ).values_list('username', flat=True).distinct())

            with schema_context(get_public_schema_name()):
                records = [
                    BillableClientRecord(cycle=cycle, username=uname)
                    for uname in active_usernames if uname
                ]
                if records:
                    created = BillableClientRecord.objects.bulk_create(records, ignore_conflicts=True)
                    total_created += len(created)

        except Exception as e:
            logger.error(f"Ghost record sweep failed for {cycle.tenant.name}: {e}")

    return f"Ghost record sweep: {total_created} new records across {active_cycles.count()} cycles."


# ═══════════════════════════════════════════════════════════════════════════
# HELPER: Get Tenant for a Company
# ═══════════════════════════════════════════════════════════════════════════

def _get_tenant_for_company(company):
    """Resolve the Tenant record for a Company."""
    from apps.core.models import Tenant
    try:
        return Tenant.objects.get(company=company)
    except Tenant.DoesNotExist:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# One-shot email tasks (called from views/signals, not beat-scheduled)
# ═══════════════════════════════════════════════════════════════════════════

@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_cycle_activated_email(self, company_id):
    """Send a cycle-activation confirmation email when subscription payment succeeds."""
    from apps.core.models import Company
    try:
        company = Company.objects.select_related('subscription', 'subscription__plan').get(id=company_id)
        tenant = _get_tenant_for_company(company)
        if not tenant:
            logger.warning(f"No tenant for company {company_id}, skipping cycle_activated email")
            return

        sub = company.subscription
        cycle = BillingCycle.objects.filter(
            subscription=sub, status='active'
        ).order_by('-start_date').first()

        # Get the most recent completed payment for context
        latest_payment = sub.payments.filter(status='completed').order_by('-completed_at').first()

        context = {
            'plan_name': sub.plan.name if sub.plan else 'Metered',
            'cycle_start': cycle.start_date if cycle else sub.current_period_start,
            'cycle_end': cycle.end_date if cycle else sub.current_period_end,
            'base_fee': str(sub.plan.base_license_fee) if sub.plan else '500',
            'amount_paid': str(latest_payment.amount) if latest_payment else '',
            'mpesa_receipt': latest_payment.mpesa_receipt if latest_payment else '',
        }
        _send_lifecycle_email(
            tenant,
            'emails/billing/cycle_activated.html',
            'Payment Received — Your Netily Cycle is Active',
            context,
        )
    except Company.DoesNotExist:
        logger.error(f"Company {company_id} not found for cycle_activated email")
    except Exception as exc:
        logger.error(f"send_cycle_activated_email failed for company {company_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_trial_welcome_email(self, company_id):
    """Send a welcome email when a new ISP signs up and gets a trial."""
    from apps.core.models import Company
    try:
        company = Company.objects.select_related('subscription').get(id=company_id)
        tenant = _get_tenant_for_company(company)
        if not tenant:
            logger.warning(f"No tenant for company {company_id}, skipping trial_welcome email")
            return

        sub = company.subscription
        context = {
            'trial_ends_at': sub.trial_ends_at,
            'trial_duration_days': sub.TRIAL_DURATION_DAYS,
            'base_fee': str(sub.plan.base_license_fee) if sub.plan else '500',
        }
        _send_lifecycle_email(
            tenant,
            'emails/billing/trial_welcome.html',
            f'Welcome to Netily! Your {sub.TRIAL_DURATION_DAYS}-Day Free Trial Starts Now',
            context,
        )
    except Company.DoesNotExist:
        logger.error(f"Company {company_id} not found for trial_welcome email")
    except Exception as exc:
        logger.error(f"send_trial_welcome_email failed for company {company_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task
def reconcile_hotspot_accumulators():
    """
    Periodic task: recalculate hotspot_revenue_accumulated from actual
    HotspotSession records for all active billing cycles.

    This ensures the real-time accumulator stays in sync with the DB
    source of truth, catching any drift from bugs or race conditions.
    """
    from apps.billing.models.hotspot_models import HotspotSession

    active_cycles = BillingCycle.objects.filter(
        status='active',
    ).select_related('tenant')

    reconciled = 0
    for cycle in active_cycles:
        try:
            with schema_context(cycle.tenant.schema_name):
                actual = HotspotSession.objects.filter(
                    status__in=['active', 'expired'],
                    activated_at__gte=cycle.start_date,
                    activated_at__lt=cycle.end_date,
                ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

            with schema_context(get_public_schema_name()):
                if cycle.hotspot_revenue_accumulated != actual:
                    logger.info(
                        f"[{cycle.tenant.schema_name}] Reconciling hotspot accumulator: "
                        f"KES {cycle.hotspot_revenue_accumulated} -> KES {actual}"
                    )
                    BillingCycle.objects.filter(pk=cycle.pk).update(
                        hotspot_revenue_accumulated=actual
                    )
                    reconciled += 1
        except Exception as e:
            logger.error(f"[{cycle.tenant.schema_name}] Reconciliation failed: {e}")

    return f"Reconciled {reconciled}/{active_cycles.count()} active cycles"


@shared_task(name='apps.subscriptions.tasks.refresh_metered_billing_estimates')
def refresh_metered_billing_estimates():
    """
    Periodic task: pre-compute metered billing estimates for all active
    metered-plan tenants and cache them in Redis for 8 hours.

    Scheduled 3× / day (see config/celery.py beat_schedule).
    """
    from django.core.cache import cache
    from apps.core.models import Company

    active_companies = Company.objects.filter(
        subscription__plan__is_metered=True,
        subscription__status__in=['active', 'trialing'],
    ).select_related('subscription__plan')

    refreshed = 0
    for company in active_companies:
        try:
            plan = company.subscription.plan
            schema = company.schema_name if hasattr(company, 'schema_name') else None
            if not schema:
                continue

            with schema_context(schema):
                from apps.customers.models import Customer
                pppoe_count = Customer.objects.count()

            pppoe_min = int(plan.pppoe_min_clients)
            pppoe_unit = Decimal(str(plan.pppoe_unit_price))
            base_fee = Decimal(str(plan.base_license_fee))
            hotspot_share_pct = Decimal(str(plan.hotspot_revenue_share_pct))
            billable_pppoe = max(pppoe_count, pppoe_min)
            pppoe_charge = Decimal(billable_pppoe) * pppoe_unit
            total_estimate = base_fee + pppoe_charge

            data = {
                'is_metered': True,
                'plan_name': plan.name,
                'base_fee': str(base_fee),
                'pppoe_count': pppoe_count,
                'pppoe_min_clients': pppoe_min,
                'pppoe_unit_price': str(pppoe_unit),
                'billable_pppoe': billable_pppoe,
                'pppoe_charge': str(pppoe_charge),
                'hotspot_share_pct': str(hotspot_share_pct),
                'total_estimate': str(total_estimate),
                'note': 'Hotspot revenue share calculated at cycle close and not included in total_estimate.',
            }
            cache_key = f'metered_estimate:{company.pk}'
            cache.set(cache_key, data, timeout=60 * 60 * 9)  # 9-hour TTL (> 8 h so never cold)
            refreshed += 1
        except Exception as exc:
            logger.error(f"refresh_metered_billing_estimates failed for company {company.pk}: {exc}")

    return f"Refreshed metered estimate cache for {refreshed} tenant(s)"
