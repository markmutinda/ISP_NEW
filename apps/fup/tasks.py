import logging
from celery import shared_task
from django_tenants.utils import schema_context, get_tenant_model
from django.db import models
from django.utils import timezone
from .services import FUPUsageService, FUPEnforcementService

logger = logging.getLogger(__name__)

@shared_task
def sync_fup_usage():
    """Aggregates traffic for ALL tenants — both PPPoE and hotspot."""
    TenantModel = get_tenant_model()
    tenants = TenantModel.objects.exclude(schema_name='public')
    total_synced = 0
    
    for tenant in tenants:
        with schema_context(tenant.schema_name):
            try:
                service = FUPUsageService()
                synced = service.sync_usage_for_all_active_services()
                # NEW: also sync hotspot sessions
                synced += service.sync_usage_for_all_active_hotspot_sessions()
                total_synced += synced
                if synced > 0:
                    logger.info(f"[FUP SYNC] {synced} services/sessions updated for tenant {tenant.schema_name}")
            except Exception as e:
                logger.error(f"[FUP SYNC] Error in tenant {tenant.schema_name}: {e}")
                
    return {'total_synced': total_synced}

@shared_task
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

@shared_task
def reconcile_fup_states():
    """
    Hourly: release throttles where the current usage window is within limits.
    
    This is CRITICAL for period resets (daily/weekly/monthly) where a user
    was throttled in the previous period but their usage is now within limits
    in the new period. Without this, they would stay throttled forever.
    """
    from apps.fup.models import FUPThrottleState, FUPUsageWindow
    from apps.fup.services import FUPEnforcementService
    from django.utils import timezone
    
    TenantModel = get_tenant_model()
    tenants = TenantModel.objects.exclude(schema_name='public')
    total_released = 0
    
    for tenant in tenants:
        try:
            with schema_context(tenant.schema_name):
                released_count = 0
                now = timezone.now()
                
                # Get all active throttles (both PPPoE and Hotspot)
                active_throttles = FUPThrottleState.objects.filter(
                    active=True
                ).select_related('service_connection', 'hotspot_session', 'policy')
                
                service = FUPEnforcementService()
                
                for ts in active_throttles:
                    # Find the current usage window for this throttle
                    current_window = None
                    
                    if ts.service_connection:
                        # PPPoE / Static connection
                        current_window = FUPUsageWindow.objects.filter(
                            policy=ts.policy,
                            service_connection=ts.service_connection,
                            period_start__lte=now,
                            period_end__gt=now,
                        ).first()
                    elif ts.hotspot_session:
                        # Hotspot session
                        current_window = FUPUsageWindow.objects.filter(
                            policy=ts.policy,
                            hotspot_session=ts.hotspot_session,
                            period_start__lte=now,
                            period_end__gt=now,
                        ).first()
                    else:
                        # No connection or session - orphaned throttle, deactivate it
                        logger.warning(f"FUP throttle {ts.id} has no associated service_connection or hotspot_session")
                        ts.active = False
                        ts.released_at = now
                        ts.reason = 'Orphaned throttle - no associated connection'
                        ts.save(update_fields=['active', 'released_at', 'reason'])
                        continue
                    
                    # Check if we should release the throttle
                    should_release = False
                    release_reason = ''
                    
                    if not current_window:
                        # No current window exists - this could be a period gap
                        # Better to release to be safe
                        should_release = True
                        release_reason = 'No current usage window found'
                        logger.warning(f"Throttle {ts.id} has no current window, releasing")
                    elif current_window.total_bytes <= current_window.limit_bytes:
                        # Usage is within limit in the current period
                        should_release = True
                        release_reason = f'Period reset - usage within limit ({current_window.total_gb} GB / {current_window.limit_gb} GB)'
                    
                    if should_release:
                        try:
                            if ts.service_connection:
                                # Release PPPoE service throttle
                                service.release_service(
                                    ts.service_connection, 
                                    reason=release_reason
                                )
                                released_count += 1
                                logger.info(
                                    f"[FUP RECONCILE] Released throttle for service {ts.service_connection.id} "
                                    f"in tenant {tenant.schema_name}: {release_reason}"
                                )
                            elif ts.hotspot_session:
                                # Release Hotspot session throttle
                                service._release_hotspot_throttle(
                                    ts.hotspot_session, 
                                    ts, 
                                    release_reason
                                )
                                released_count += 1
                                logger.info(
                                    f"[FUP RECONCILE] Released throttle for hotspot session {ts.hotspot_session.id} "
                                    f"in tenant {tenant.schema_name}: {release_reason}"
                                )
                        except Exception as e:
                            logger.error(
                                f"[FUP RECONCILE] Failed to release throttle {ts.id} "
                                f"in tenant {tenant.schema_name}: {e}"
                            )
                
                total_released += released_count
                if released_count > 0:
                    logger.info(
                        f"[FUP RECONCILE] Released {released_count} throttles for tenant {tenant.schema_name}"
                    )
                    
        except Exception as e:
            logger.error(f"[FUP RECONCILE] Error in tenant {tenant.schema_name}: {e}", exc_info=True)
    
    logger.info(f"[FUP RECONCILE] Complete: {total_released} throttles released across all tenants")
    return {'status': 'ok', 'released': total_released}


@shared_task
def reconcile_fup_states_for_tenant(schema_name: str):
    """
    Tenant-specific reconciliation task - useful for manual triggers.
    
    Args:
        schema_name: The tenant schema to reconcile
    """
    from apps.fup.models import FUPThrottleState, FUPUsageWindow
    from apps.fup.services import FUPEnforcementService
    from django.utils import timezone
    
    released_count = 0
    
    try:
        with schema_context(schema_name):
            now = timezone.now()
            
            active_throttles = FUPThrottleState.objects.filter(
                active=True
            ).select_related('service_connection', 'hotspot_session', 'policy')
            
            service = FUPEnforcementService()
            
            for ts in active_throttles:
                current_window = None
                
                if ts.service_connection:
                    current_window = FUPUsageWindow.objects.filter(
                        policy=ts.policy,
                        service_connection=ts.service_connection,
                        period_start__lte=now,
                        period_end__gt=now,
                    ).first()
                elif ts.hotspot_session:
                    current_window = FUPUsageWindow.objects.filter(
                        policy=ts.policy,
                        hotspot_session=ts.hotspot_session,
                        period_start__lte=now,
                        period_end__gt=now,
                    ).first()
                else:
                    ts.active = False
                    ts.released_at = now
                    ts.reason = 'Orphaned throttle - no associated connection'
                    ts.save(update_fields=['active', 'released_at', 'reason'])
                    continue
                
                if not current_window or current_window.total_bytes <= current_window.limit_bytes:
                    if ts.service_connection:
                        service.release_service(
                            ts.service_connection, 
                            reason='Period reset - within limits'
                        )
                        released_count += 1
                    elif ts.hotspot_session:
                        service._release_hotspot_throttle(
                            ts.hotspot_session, 
                            ts, 
                            'Period reset - within limits'
                        )
                        released_count += 1
            
            if released_count > 0:
                logger.info(
                    f"[FUP RECONCILE] Released {released_count} throttles for tenant {schema_name}"
                )
                
    except Exception as e:
        logger.error(f"[FUP RECONCILE] Error in tenant {schema_name}: {e}", exc_info=True)
        raise
    
    return {'schema': schema_name, 'released': released_count}