import logging
import subprocess
import time
from celery import shared_task
from django_tenants.utils import get_tenant_model, schema_context
from apps.network.models.router_models import Router

logger = logging.getLogger(__name__)


@shared_task
def refresh_router_statuses():
    """
    Background refresh of router status for all tenants.
    Replaces synchronous status checks from API request path.
    """
    TenantModel = get_tenant_model()
    tenants = TenantModel.objects.exclude(schema_name='public')

    total = 0
    errors = 0
    started = time.perf_counter()

    for tenant in tenants:
        tenant_start = time.perf_counter()
        try:
            with schema_context(tenant.schema_name):
                # Removed .only() so the entire router object (including auth_key) loads into memory
                routers = Router.objects.filter(is_active=True)

                for router in routers:
                    try:
                        router.sync_status(force=True)
                        total += 1
                    except Exception:
                        errors += 1

            dur_ms = int((time.perf_counter() - tenant_start) * 1000)
            logger.info(f"[ROUTER STATUS] tenant={tenant.schema_name} duration_ms={dur_ms}")

        except Exception as e:
            errors += 1
            logger.error(f"[ROUTER STATUS] tenant={tenant.schema_name} error={e}")

    total_ms = int((time.perf_counter() - started) * 1000)
    logger.info(f"[ROUTER STATUS] complete refreshed={total} errors={errors} duration_ms={total_ms}")
    return {"refreshed": total, "errors": errors, "duration_ms": total_ms}


@shared_task(
    bind=True, 
    queue="celery",
    autoretry_for=(Exception,), 
    retry_backoff=True, 
    max_retries=5
)
def reload_radius_clients(self):
    """
    Reload FreeRADIUS clients list so new GlobalRouterMap entries are recognized.
    
    NOTE: This task is kept for manual/admin use only.
    Automatic reloads on router changes now happen synchronously via 
    transaction.on_commit(reload_radius_clients_now) in signals.py
    """
    cmd = ["docker", "exec", "netily_radius", "radmin", "-e", "hup"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)

    if result.returncode != 0:
        raise RuntimeError(f"radmin hup failed: {result.stderr.strip()}")

    logger.info("Triggered FreeRADIUS HUP to reload clients.")
    return {"ok": True, "stdout": result.stdout.strip()}


@shared_task(bind=True, queue='default', max_retries=3)
def populate_ip_pool_addresses(self, pool_id: int):
    """
    Background task to generate IPAddress records for large pools.
    Called when pool has > 1000 IPs to avoid blocking the HTTP request.
    """
    try:
        from apps.network.models.ipam_models import IPPool
        pool = IPPool.objects.get(id=pool_id)
        pool._populate_ip_addresses()
        logger.info(f"[TASK] IPPool '{pool.name}' (id={pool_id}): IP generation complete")
    except IPPool.DoesNotExist:
        logger.error(f"[TASK] IPPool id={pool_id} not found")
    except Exception as exc:
        logger.error(f"[TASK] IPPool id={pool_id} IP generation failed: {exc}")
        raise self.retry(exc=exc, countdown=30)