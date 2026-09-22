import logging
from celery import shared_task, group
from django_tenants.utils import schema_context, get_tenant_model
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name='apps.fup.tasks.fup_delta_sync_fanout')
def fup_delta_sync_fanout():
    """Fan out one delta-sync task per tenant."""
    TenantModel = get_tenant_model()
    schemas = list(
        TenantModel.objects.exclude(schema_name='public').values_list('schema_name', flat=True)
    )
    group(fup_delta_sync_for_tenant.s(schema) for schema in schemas).apply_async()
    return {'tenants_dispatched': len(schemas)}


@shared_task(
    name='apps.fup.tasks.fup_delta_sync_for_tenant',
    bind=True,
    max_retries=2,
    default_retry_delay=10,
)
def fup_delta_sync_for_tenant(self, schema_name: str):
    from apps.fup.services.delta_sync_service import FUPDeltaSyncService

    lock_key = f"fup_delta_sync_lock:{schema_name}"
    if not cache.add(lock_key, "1", timeout=55):
        logger.info(f"[FUP DELTA] {schema_name}: previous run still active, skipping")
        return {'skipped': True}
    try:
        with schema_context(schema_name):
            result = FUPDeltaSyncService().run()
            if result.get('windows_updated'):
                logger.info(f"[FUP DELTA] {schema_name}: {result}")
            return result
    except Exception as exc:
        logger.error(f"[FUP DELTA] {schema_name} failed: {exc}")
        raise self.retry(exc=exc)
    finally:
        cache.delete(lock_key)


@shared_task(name='apps.fup.tasks.reconcile_fup_states')
def reconcile_fup_states():
    """Fan out one reconcile task per tenant."""
    TenantModel = get_tenant_model()
    schemas = list(
        TenantModel.objects.exclude(schema_name='public').values_list('schema_name', flat=True)
    )
    group(reconcile_fup_states_for_tenant.s(s) for s in schemas).apply_async()
    return {'tenants_dispatched': len(schemas)}


@shared_task(
    name='apps.fup.tasks.reconcile_fup_states_for_tenant',
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def reconcile_fup_states_for_tenant(self, schema_name: str):
    from apps.fup.models import FUPThrottleState, FUPUsageWindow
    from apps.fup.services import FUPEnforcementService

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
        raise self.retry(exc=e)

    return {'schema': schema_name, 'released': released_count}