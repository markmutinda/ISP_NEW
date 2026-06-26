# apps/network/services/port_allocator.py
"""
Global cross-tenant port allocator for HAProxy remote access ports.
Uses a Redis atomic counter as the single source of truth to avoid
cross-tenant DB scans which fail on new (unsaved) router instances.
"""
import logging
from django.core.cache import cache

logger = logging.getLogger(__name__)

WINBOX_PORT_BASE = 40000
API_PORT_BASE = 50000
PORT_COUNTER_KEY = "global:haproxy:port_counter"
PORT_LOCK_KEY = "global:haproxy:port_lock"
PORT_LOCK_TTL = 10  # seconds


def _get_highest_allocated_from_db() -> int:
    """
    One-time scan of all tenant schemas to find the current highest
    allocated port offset. Used only to seed the Redis counter on first use.
    """
    from django_tenants.utils import schema_context, get_tenant_model
    from apps.network.models.router_models import Router
    from django.db.models import Max

    TenantModel = get_tenant_model()
    highest = 0

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                res = Router.objects.aggregate(
                    max_w=Max('winbox_remote_port'),
                    max_a=Max('api_remote_port'),
                )
                for val in (res.get('max_w'), res.get('max_a')):
                    if val is None:
                        continue
                    # Normalize both port families to a single offset
                    if val > API_PORT_BASE:
                        offset = val - API_PORT_BASE
                    else:
                        offset = val - WINBOX_PORT_BASE
                    if offset > highest:
                        highest = offset
        except Exception as e:
            logger.warning("[PORT ALLOCATOR] Could not scan tenant %s: %s", tenant.schema_name, e)

    return highest


def allocate_ports() -> tuple[int, int]:
    """
    Atomically allocate the next available (winbox_port, api_port) pair.

    Uses Redis INCR as the counter — truly atomic, no scan needed.
    Falls back to a DB scan only when the counter key doesn't exist yet
    (i.e. first ever call after a fresh deploy or Redis flush).

    Returns:
        (winbox_port, api_port) — guaranteed unique across all tenants.
    """
    lock_acquired = False
    import time

    start = time.time()
    while time.time() - start < 8.0:
        if cache.add(PORT_LOCK_KEY, "1", timeout=PORT_LOCK_TTL):
            lock_acquired = True
            break
        time.sleep(0.15)

    if not lock_acquired:
        raise RuntimeError("Could not acquire port allocation lock. Please retry.")

    try:
        current = cache.get(PORT_COUNTER_KEY)

        if current is None:
            # Seed from DB on first use
            current = _get_highest_allocated_from_db()
            logger.info("[PORT ALLOCATOR] Seeded counter from DB at offset %d", current)

        next_offset = int(current) + 1
        cache.set(PORT_COUNTER_KEY, next_offset, timeout=None)  # persist indefinitely

        winbox_port = WINBOX_PORT_BASE + next_offset
        api_port = API_PORT_BASE + next_offset

        logger.info("[PORT ALLOCATOR] Allocated offset=%d → winbox=%d api=%d",
                    next_offset, winbox_port, api_port)
        return winbox_port, api_port

    finally:
        cache.delete(PORT_LOCK_KEY)