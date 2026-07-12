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
    
    OPTIMIZED: Fetches WireGuard peer table once per sweep to avoid
    N docker exec calls for WG-provisioned routers.
    """
    from apps.vpn.services.wireguard_manager import list_connected_peers

    # Fetch WG peer table once for the whole sweep (avoids N docker execs)
    try:
        peers = list_connected_peers()
        handshake_map = {p['public_key']: p.get('latest_handshake') or 0 for p in peers}
    except Exception as e:
        logger.warning(f"[ROUTER STATUS] Could not list WG peers, falling back to TCP for all: {e}")
        handshake_map = {}

    now = time.time()
    TenantModel = get_tenant_model()
    tenants = TenantModel.objects.exclude(schema_name='public')

    total = 0
    errors = 0
    started = time.perf_counter()

    for tenant in tenants:
        tenant_start = time.perf_counter()
        try:
            with schema_context(tenant.schema_name):
                routers = Router.objects.filter(is_active=True)

                for router in routers:
                    try:
                        # Use pre-fetched handshake age if this is a WG router
                        if router.vpn_provisioned and router.wireguard_public_key and router.wireguard_public_key in handshake_map:
                            latest = handshake_map[router.wireguard_public_key]
                            age = (now - latest) if latest > 0 else None
                            router.sync_status(force=True, peer_handshake_age=age)
                        else:
                            router.sync_status(force=True)  # TCP fallback path, unchanged
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


@shared_task
def watchdog_router_status():
    """
    Fast, frequent status sweep for WireGuard-provisioned routers.
    Fetches the WG peer table once, then updates all routers' status
    from handshake age — no per-router network calls.
    """
    from apps.vpn.services.wireguard_manager import list_connected_peers

    try:
        peers = list_connected_peers()
    except Exception as e:
        logger.error(f"[WATCHDOG] Could not list WG peers: {e}")
        return {'error': str(e)}

    now = time.time()
    handshake_map = {p['public_key']: p.get('latest_handshake') or 0 for p in peers}

    TenantModel = get_tenant_model()
    flipped = 0
    checked = 0

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                routers = Router.objects.filter(
                    is_active=True,
                    vpn_provisioned=True,
                    wireguard_public_key__isnull=False,
                ).exclude(wireguard_public_key='')

                for router in routers:
                    latest = handshake_map.get(router.wireguard_public_key, 0)
                    age = (now - latest) if latest > 0 else None
                    old_status = router.status
                    router.sync_status(force=True, peer_handshake_age=age)
                    checked += 1
                    if router.status != old_status:
                        flipped += 1
        except Exception as e:
            logger.error(f"[WATCHDOG] tenant={tenant.schema_name} error={e}")

    logger.info(f"[WATCHDOG] checked={checked} flipped={flipped}")
    return {'checked': checked, 'flipped': flipped}


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
def populate_ip_pool_addresses(self, pool_id: int, schema_name: str = 'public'):
    """
    Generate IPAddress records for a large pool.
    Must receive schema_name so the worker switches to the correct tenant schema.
    
    Called when pool has > 1000 IPs to avoid blocking the HTTP request.
    
    Args:
        pool_id: The ID of the IPPool to populate
        schema_name: The tenant schema name where the pool exists
    
    Returns:
        dict: Result status and details
    """
    # Guard: never run against public schema (IPPool doesn't exist there)
    if schema_name == 'public':
        logger.error(
            f"[TASK] populate_ip_pool_addresses called with schema='public' "
            f"for pool_id={pool_id}. Aborting."
        )
        return {'status': 'aborted', 'reason': 'public_schema', 'pool_id': pool_id}

    try:
        # Switch to the correct tenant schema before accessing the model
        with schema_context(schema_name):
            from apps.network.models.ipam_models import IPPool
            
            # Fetch the pool
            pool = IPPool.objects.get(id=pool_id)
            
            # Perform the IP address population
            pool._populate_ip_addresses()
            
            logger.info(
                f"[TASK] IPPool '{pool.name}' (id={pool_id}) schema={schema_name}: "
                f"IP generation complete"
            )
            
            return {
                'status': 'done',
                'pool_id': pool_id,
                'schema': schema_name,
                'pool_name': pool.name
            }
            
    except IPPool.DoesNotExist:
        logger.error(f"[TASK] IPPool id={pool_id} schema={schema_name} not found")
        return {
            'status': 'error',
            'reason': 'not_found',
            'pool_id': pool_id,
            'schema': schema_name
        }
        
    except Exception as exc:
        logger.error(f"[TASK] IPPool id={pool_id} schema={schema_name} failed: {exc}")
        raise self.retry(exc=exc, countdown=30)


# ===== NEW TASK: HAProxy Config Sync =====
@shared_task(
    bind=True,
    queue='default',
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3
)
def sync_haproxy_config_task(self):
    """
    Synchronize HAProxy configuration asynchronously.
    This task runs outside of database transactions to prevent
    long-running Docker socket calls from holding DB connections.
    
    Called via transaction.on_commit() from Router.save() to ensure
    it runs after the transaction commits.
    """
    from apps.network.services.haproxy_manager import sync_haproxy_config
    
    try:
        logger.info("[HAPROXY] Starting async config sync")
        sync_haproxy_config()
        logger.info("[HAPROXY] Config sync completed successfully")
        return {"ok": True, "message": "HAProxy config synced"}
    except Exception as e:
        logger.error(f"[HAPROXY] Config sync failed: {e}")
        raise self.retry(exc=e, countdown=10)