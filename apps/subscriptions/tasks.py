import logging
from itertools import islice
from datetime import timedelta
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django_tenants.utils import schema_context, get_public_schema_name

from apps.subscriptions.models import BillingCycle, BillableClientRecord

logger = logging.getLogger(__name__)

def batched(iterable, n):
    """Helper to batch iterators into chunks to prevent memory bloat"""
    it = iter(iterable)
    while batch := tuple(islice(it, n)):
        yield batch

@shared_task
def generate_metered_invoices():
    now = timezone.now()
    
    # ── FIX 4.1 & 3.2: OPTIMIZED QUERY & TRIAL EXCLUSION ──
    # Exclude trialing status explicitly and use select_related to prevent N+1 queries
    ended_cycles = BillingCycle.objects.filter(
        status='active',
        end_date__lte=now
    ).exclude(
        subscription__status='trialing'  # Do not bill active trials
    ).select_related('tenant', 'subscription', 'subscription__plan')
    
    if not ended_cycles.exists():
        return "No cycles to process"

    for cycle in ended_cycles:
        tenant = cycle.tenant
        
        try:
            # transaction.atomic() ensures the invoice and rollover succeed or fail together
            with transaction.atomic():
                with schema_context(get_public_schema_name()):
                    
                    total_due = cycle.calculate_total_charge()
                    pppoe_count = cycle.calculate_total_pppoe()
                    hotspot_rev = cycle.hotspot_revenue_accumulated
                    
                    logger.info(f"[{tenant.name}] Cycle Ended. PPPoE: {pppoe_count}, Hotspot Rev: {hotspot_rev}. Total Bill: KES {total_due}")
                    
                    # Update status
                    cycle.status = 'invoiced'
                    cycle.save(update_fields=['status'])
                    
                    # Create the new cycle (Prices automatically snapshotted via the overridden save method)
                    new_cycle = BillingCycle.objects.create(
                        tenant=tenant,
                        subscription=cycle.subscription,
                        start_date=now,
                        end_date=now + timedelta(days=30),
                        status='active'
                    )
                
                # ── FIX 4.2: MEMORY EFFICIENT CARRY FORWARD ──
                with schema_context(tenant.schema_name):
                    from apps.radius.models import CustomerRadiusCredentials
                    
                    # Use .iterator() to stream results from the DB instead of loading all to RAM
                    active_creds_iterator = CustomerRadiusCredentials.objects.filter(
                        is_enabled=True,
                        connection_type__in=['PPPOE', 'BOTH']
                    ).values_list('username', flat=True).iterator(chunk_size=1000)
                
                with schema_context(get_public_schema_name()):
                    # Process in batches of 1000 to keep memory footprint flat
                    total_carried = 0
                    for username_batch in batched(active_creds_iterator, 1000):
                        records = [
                            BillableClientRecord(cycle=new_cycle, username=uname)
                            for uname in username_batch
                        ]
                        # ignore_conflicts=True prevents failure if a race condition added them already
                        BillableClientRecord.objects.bulk_create(records, batch_size=1000, ignore_conflicts=True)
                        total_carried += len(records)
                        
                    logger.info(f"[{tenant.name}] Rolled over {total_carried} active PPPoE clients.")

        except Exception as e:
            logger.error(f"Failed to process billing cycle for tenant {tenant.name}: {str(e)}")

    return f"Processed {ended_cycles.count()} billing cycles."