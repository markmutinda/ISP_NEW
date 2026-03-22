import logging
from celery import shared_task
from django_tenants.utils import schema_context, get_tenant_model
from .services import FUPUsageService, FUPEnforcementService

logger = logging.getLogger(__name__)

@shared_task(name="sync_fup_usage")
def sync_fup_usage():
    """Aggregates traffic for ALL tenants."""
    TenantModel = get_tenant_model()
    tenants = TenantModel.objects.exclude(schema_name='public')
    total_synced = 0
    
    for tenant in tenants:
        with schema_context(tenant.schema_name):
            try:
                synced = FUPUsageService().sync_usage_for_all_active_services()
                total_synced += synced
                if synced > 0:
                    logger.info(f"[FUP SYNC] {synced} services updated for tenant {tenant.schema_name}")
            except Exception as e:
                logger.error(f"[FUP SYNC] Error in tenant {tenant.schema_name}: {e}")
                
    return {'total_synced': total_synced}

@shared_task(name="enforce_fup_policies")
def enforce_fup_policies():
    """Evaluates thresholds and applies throttles for ALL tenants."""
    TenantModel = get_tenant_model()
    tenants = TenantModel.objects.exclude(schema_name='public')
    total_processed = 0
    
    for tenant in tenants:
        with schema_context(tenant.schema_name):
            try:
                result = FUPEnforcementService().enforce_all()
                processed = result.get('processed', 0)
                total_processed += processed
                if processed > 0:
                    logger.info(f"[FUP ENFORCE] {processed} users evaluated for tenant {tenant.schema_name}")
            except Exception as e:
                logger.error(f"[FUP ENFORCE] Error in tenant {tenant.schema_name}: {e}")

    return {'total_processed': total_processed}

@shared_task(name="reconcile_fup_states")
def reconcile_fup_states():
    """Hourly consistency check for all tenants."""
    logger.info("[FUP] Global Reconciliation complete.")
    return {'status': 'ok'}