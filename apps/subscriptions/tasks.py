# apps/subscriptions/tasks.py
import logging
from itertools import islice
from datetime import timedelta
from decimal import Decimal
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
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
                with schema_context(tenant.schema_name):
                    # Find all unique usernames that had an active session during this specific cycle
                    active_usernames = list(RadAcct.objects.filter(
                        acctstarttime__lt=cycle.end_date
                    ).filter(
                        Q(acctstoptime__isnull=True) | Q(acctstoptime__gt=cycle.start_date)
                    ).values_list('username', flat=True).distinct())
                
                # Switch to public schema to insert the ghost records for billing
                with schema_context(get_public_schema_name()):
                    records_to_create = [
                        BillableClientRecord(cycle=cycle, username=uname)
                        for uname in active_usernames if uname
                    ]
                    if records_to_create:
                        BillableClientRecord.objects.bulk_create(records_to_create, ignore_conflicts=True)
                
                    # ─── PHASE 3: THE EXACT MATH ───
                    base_fee = cycle.snapshot_base_fee
                    pppoe_count = cycle.calculate_total_pppoe()
                    pppoe_fee = cycle.calculate_pppoe_charge()
                    
                    hotspot_share = (cycle.hotspot_revenue_accumulated * (cycle.snapshot_hotspot_share_pct / Decimal('100.0'))).quantize(Decimal('0.01'))
                    total_due = cycle.calculate_total_charge()
                    
                    # ─── PHASE 4: THE ENFORCEMENT LOOP (LOCKOUT) ───
                    # We DO NOT create a new cycle. We freeze the account until paid.
                    sub = cycle.subscription
                    sub.status = 'past_due'
                    sub.save(update_fields=['status'])
                    
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
                        due_date=(now + timedelta(days=7)).date(),
                        billing_date=now.date(),
                    )

                    # Create Exact Line Items (Breakdown)
                    InvoiceItem.objects.create(
                        invoice=new_invoice, description='Netily Platform - Base License Fee',
                        quantity=1, unit_price=base_fee, tax_rate=0, tax_amount=0, total=base_fee
                    )
                    if pppoe_fee > 0:
                        InvoiceItem.objects.create(
                            invoice=new_invoice, description=f'Active PPPoE Clients ({pppoe_count} users @ KES {cycle.snapshot_pppoe_price})',
                            quantity=1, unit_price=pppoe_fee, tax_rate=0, tax_amount=0, total=pppoe_fee
                        )
                    if hotspot_share > 0:
                        InvoiceItem.objects.create(
                            invoice=new_invoice, description='Hotspot Revenue Share (3%)',
                            quantity=1, unit_price=hotspot_share, tax_rate=0, tax_amount=0, total=hotspot_share
                        )

                # Save Invoice Reference in Public Schema
                with schema_context(get_public_schema_name()):
                    cycle.status = 'invoiced'
                    cycle.invoice_reference = str(new_invoice.id)
                    cycle.save(update_fields=['status', 'invoice_reference'])
                    
                    logger.info(f"[{tenant.name}] Invoiced KES {total_due} (Invoice #{new_invoice.id}). Billed {pppoe_count} true PPPoE users. Subscription locked until paid.")

        except Exception as e:
            logger.error(f"Failed to process billing cycle for tenant {tenant.name}: {str(e)}")

    return f"Processed {ended_cycles.count()} billing cycles."