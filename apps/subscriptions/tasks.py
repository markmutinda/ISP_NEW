# apps/subscriptions/tasks.py

import logging
from datetime import timedelta
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django_tenants.utils import schema_context, get_public_schema_name

from apps.subscriptions.models import BillingCycle, BillableClientRecord
from apps.core.models import Tenant

logger = logging.getLogger(__name__)

@shared_task
def generate_metered_invoices():
    """
    Runs daily to close out 30-day billing cycles, generate the ISP's invoice,
    and create the next 30-day cycle with 'carried forward' PPPoE users.
    """
    now = timezone.now()
    
    # 1. Find all billing cycles that ended today (or earlier) and are still 'active'
    ended_cycles = BillingCycle.objects.filter(
        status='active',
        end_date__lte=now
    )
    
    if not ended_cycles.exists():
        logger.info("No billing cycles ended today.")
        return "No cycles to process"

    # Process each ISP's cycle
    for cycle in ended_cycles:
        tenant = cycle.tenant
        
        try:
            with transaction.atomic():
                with schema_context(get_public_schema_name()):
                    
                    # ── A. CALCULATE THE BILL ──
                    total_due = cycle.calculate_total_charge()
                    pppoe_count = cycle.calculate_total_pppoe()
                    hotspot_rev = cycle.hotspot_revenue_accumulated
                    
                    logger.info(f"[{tenant.name}] Cycle Ended. PPPoE: {pppoe_count}, Hotspot Rev: {hotspot_rev}. Total Bill: KES {total_due}")
                    
                    # NOTE: Here you would create the actual Invoice record for the ISP to pay
                    # Example: PlatformInvoice.objects.create(tenant=tenant, amount=total_due...)
                    
                    # Mark this cycle as invoiced
                    cycle.status = 'invoiced'
                    cycle.save()
                    
                    # ── B. CREATE THE NEW 30-DAY CYCLE ──
                    new_start = now
                    new_end = now + timedelta(days=30)
                    
                    new_cycle = BillingCycle.objects.create(
                        tenant=tenant,
                        subscription=cycle.subscription,
                        start_date=new_start,
                        end_date=new_end,
                        status='active'
                    )
                
                # ── C. THE "CARRY FORWARD" LOGIC ──
                # We must peek inside the ISP's database to see who is STILL active right now
                active_usernames = []
                with schema_context(tenant.schema_name):
                    # Import the tenant's radius credentials model here
                    from apps.radius.models import CustomerRadiusCredentials
                    
                    active_creds = CustomerRadiusCredentials.objects.filter(
                        is_enabled=True,
                        connection_type__in=['PPPOE', 'BOTH']
                    ).values_list('username', flat=True)
                    
                    active_usernames = list(active_creds)
                
                # ── D. POPULATE THE NEW GHOST RECORDS ──
                # Jump back to the public schema to save them permanently
                with schema_context(get_public_schema_name()):
                    records_to_create = [
                        BillableClientRecord(cycle=new_cycle, username=uname)
                        for uname in active_usernames
                    ]
                    
                    if records_to_create:
                        BillableClientRecord.objects.bulk_create(records_to_create)
                        
                    logger.info(f"[{tenant.name}] Rolled over {len(records_to_create)} active PPPoE clients to new cycle.")

        except Exception as e:
            logger.error(f"Failed to process billing cycle for tenant {tenant.name}: {str(e)}")

    return f"Processed {ended_cycles.count()} billing cycles."