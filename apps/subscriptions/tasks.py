# apps/subscriptions/tasks.py
import logging
from itertools import islice
from datetime import timedelta
from decimal import Decimal
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.contrib.auth import get_user_model
from django_tenants.utils import schema_context, get_public_schema_name

from apps.subscriptions.models import BillingCycle, BillableClientRecord
# Make sure to import your billing models inside the tenant context or locally
from apps.billing.models import Invoice, InvoiceItem
from apps.customers.models import Customer

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
        
        try:
            with transaction.atomic():
                with schema_context(get_public_schema_name()):
                    # Calculate math using the snapshotted prices
                    base_fee = cycle.snapshot_base_fee
                    pppoe_count = cycle.calculate_total_pppoe()
                    
                    min_clients = cycle.snapshot_min_clients
                    pppoe_billable = max(0, pppoe_count - min_clients)
                    pppoe_fee = pppoe_billable * cycle.snapshot_pppoe_price
                    
                    hotspot_share = cycle.hotspot_revenue_accumulated * (cycle.snapshot_hotspot_share_pct / 100)
                    total_due = base_fee + pppoe_fee + hotspot_share
                    
                    # Update Cycle status
                    cycle.status = 'invoiced'
                    cycle.save(update_fields=['status'])
                    
                    # Rollover to new cycle
                    new_start = cycle.end_date
                    new_end = new_start + timedelta(days=30)
                    new_cycle = BillingCycle.objects.create(
                        tenant=tenant,
                        subscription=cycle.subscription,
                        start_date=new_start,
                        end_date=new_end,
                        status='active'
                    )

                    # Update subscription dates
                    sub = cycle.subscription
                    sub.current_period_start = new_start
                    sub.current_period_end = new_end
                    sub.save(update_fields=['current_period_start', 'current_period_end'])

                # 2. Create the Invoice and Line Items IN THE TENANT SCHEMA
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
                    inv = Invoice.objects.create(
                        invoice_number=f'NET-BILL-{now.strftime("%y%m%d%H%M%S")}',
                        customer=sys_customer,
                        total_amount=total_due,
                        status='ISSUED',
                        service_period_start=cycle.start_date.date(),
                        service_period_end=cycle.end_date.date(),
                        due_date=(now + timedelta(days=7)).date(),
                        billing_date=now.date(),
                    )

                    # Create Line Items (Breakdown)
                    InvoiceItem.objects.create(
                        invoice=inv, description='Netily Platform - Base License Fee',
                        quantity=1, unit_price=base_fee, tax_rate=0, tax_amount=0, total=base_fee
                    )
                    if pppoe_fee > 0:
                        InvoiceItem.objects.create(
                            invoice=inv, description=f'Metered Overage: {pppoe_billable} PPPoE Clients',
                            quantity=1, unit_price=pppoe_fee, tax_rate=0, tax_amount=0, total=pppoe_fee
                        )
                    if hotspot_share > 0:
                        InvoiceItem.objects.create(
                            invoice=inv, description='Hotspot Revenue Share',
                            quantity=1, unit_price=hotspot_share, tax_rate=0, tax_amount=0, total=hotspot_share
                        )

                # 3. Carry Forward Active PPPoE Users
                with schema_context(tenant.schema_name):
                    from apps.radius.models import CustomerRadiusCredentials
                    active_creds_iterator = CustomerRadiusCredentials.objects.filter(
                        is_enabled=True,
                        connection_type__in=['PPPOE', 'BOTH']
                    ).values_list('username', flat=True).iterator(chunk_size=1000)
                
                with schema_context(get_public_schema_name()):
                    total_carried = 0
                    for username_batch in batched(active_creds_iterator, 1000):
                        records = [BillableClientRecord(cycle=new_cycle, username=uname) for uname in username_batch]
                        BillableClientRecord.objects.bulk_create(records, batch_size=1000, ignore_conflicts=True)
                        total_carried += len(records)
                        
                    logger.info(f"[{tenant.name}] Invoiced KES {total_due}. Rolled over {total_carried} active PPPoE clients.")

        except Exception as e:
            logger.error(f"Failed to process billing cycle for tenant {tenant.name}: {str(e)}")

    return f"Processed {ended_cycles.count()} billing cycles."