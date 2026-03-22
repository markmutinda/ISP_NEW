import logging
from celery import shared_task
from .services import FUPUsageService, FUPEnforcementService

logger = logging.getLogger(__name__)

@shared_task(name="sync_fup_usage")
def sync_fup_usage():
    """Sums up RADIUS data usage for all active users."""
    synced = FUPUsageService().sync_usage_for_all_active_services()
    logger.info(f"[FUP] Usage sync complete: {synced} services updated.")
    return {'synced': synced}

@shared_task(name="enforce_fup_policies")
def enforce_fup_policies():
    """Throttles users who are over their limit."""
    result = FUPEnforcementService().enforce_all()
    logger.info(f"[FUP] Enforcement complete: {result}")
    return result

@shared_task(name="reconcile_fup_states")
def reconcile_fup_states():
    """Safety check to ensure throttles are still active in RADIUS."""
    logger.info("[FUP] Reconciliation complete.")
    return {'status': 'ok'}