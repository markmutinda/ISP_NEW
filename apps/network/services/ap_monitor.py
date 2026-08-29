# apps/network/services/ap_monitor.py
"""
AP liveness monitor — one bulk MikroTik read per router per cycle,
zero per-AP network calls. Includes cache-gated polling so routers
are only hit when someone has the page open.
"""
import logging
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

STALE_AFTER_SECONDS = 150  # ~2.5x the frontend poll interval (7s)
MIN_POLL_INTERVAL_SECONDS = 5   # floor between live MikroTik reads per router
POLL_LOCK_TTL = 8               # slightly > interval, covers slow connects


def _normalize_mac(mac: str) -> str:
    return (mac or '').upper().replace('-', ':')


def _fetch_router_link_state(router):
    """Single connection, two bulk reads. Returns (bridge_macs:set, arp_by_mac:dict)."""
    import apps.network.integrations.mikrotik_api as mikrotik_api_module

    api = mikrotik_api_module.MikrotikAPI(router)
    if not api.connect():
        return None, None

    try:
        bridge_macs = set()
        try:
            for host in api._execute('/interface/bridge/host'):
                mac = _normalize_mac(host.get('mac-address', ''))
                if mac:
                    bridge_macs.add(mac)
        except Exception as e:
            logger.warning(f"[AP MONITOR] bridge/host read failed for {router.name}: {e}")

        arp_by_mac = {}
        try:
            for entry in api._execute('/ip/arp'):
                mac = _normalize_mac(entry.get('mac-address', ''))
                if not mac:
                    continue
                complete = (
                    str(entry.get('complete', 'false')).lower() == 'true'
                    or str(entry.get('status', '')).lower() in ('reachable', 'permanent')
                )
                arp_by_mac[mac] = {'ip': entry.get('address', ''), 'complete': complete}
        except Exception as e:
            logger.warning(f"[AP MONITOR] arp read failed for {router.name}: {e}")

        return bridge_macs, arp_by_mac
    finally:
        api.disconnect()


def _resolve_seen(ap, bridge_macs: set, arp_by_mac: dict) -> bool:
    mac = _normalize_mac(ap.mac_address)
    if mac in bridge_macs:
        return True
    if ap.ip_address:
        hit = arp_by_mac.get(mac)
        if hit and hit['ip'] == ap.ip_address and hit['complete']:
            return True
    return False


def poll_router_access_points(router) -> dict:
    """Refreshes every active AccessPoint under `router` with ONE connection."""
    from apps.network.models.access_point_models import AccessPoint

    aps = list(AccessPoint.objects.filter(router=router, is_active=True))
    if not aps:
        return {'router_id': router.id, 'checked': 0, 'online': 0, 'offline': 0}

    bridge_macs, arp_by_mac = _fetch_router_link_state(router)
    now = timezone.now()

    if bridge_macs is None:
        # Router unreachable — don't flip APs offline instantly;
        # only mark 'unknown' once they've been silent a while.
        cutoff = now.timestamp() - STALE_AFTER_SECONDS
        stale = [ap for ap in aps if not ap.last_seen or ap.last_seen.timestamp() < cutoff]
        for ap in stale:
            ap.status = 'unknown'
            ap.last_checked = now
        if stale:
            AccessPoint.objects.bulk_update(stale, ['status', 'last_checked'])
        return {'router_id': router.id, 'checked': len(aps), 'online': 0, 'offline': 0, 'error': 'router_unreachable'}

    online = offline = 0
    to_update = []

    for ap in aps:
        seen = _resolve_seen(ap, bridge_macs, arp_by_mac)

        if seen:
            ap.status = 'online'
            ap.last_seen = now
            online += 1
        else:
            grace_expired = not ap.last_seen or (now - ap.last_seen).total_seconds() > STALE_AFTER_SECONDS
            if grace_expired:
                ap.status = 'offline'
                offline += 1
            else:
                online += 1  # within grace window — don't flap

        ap.last_checked = now
        to_update.append(ap)

    AccessPoint.objects.bulk_update(to_update, ['status', 'last_seen', 'last_checked'])
    return {'router_id': router.id, 'checked': len(aps), 'online': online, 'offline': offline}


def get_or_refresh_router_ap_status(router) -> dict:
    """
    On-demand entry point for the frontend's status-map poll.
    - If this router was polled < MIN_POLL_INTERVAL_SECONDS ago (by ANY
      open tab/session), skip the MikroTik round-trip and just return the
      DB state — it's already fresh.
    - Otherwise, do exactly one live poll and update the cache stamp.
    - A short lock prevents two simultaneous requests (e.g. two admins
      viewing the same router) from both hitting the router at once.
    """
    cache_key = f"ap_poll:last:{router.id}"

    if cache.get(cache_key):
        return {'router_id': router.id, 'polled': False}  # served from DB, still fresh

    lock_key = f"ap_poll:lock:{router.id}"
    if not cache.add(lock_key, "1", timeout=POLL_LOCK_TTL):
        return {'router_id': router.id, 'polled': False}  # another request is polling right now

    try:
        result = poll_router_access_points(router)
        cache.set(cache_key, "1", timeout=MIN_POLL_INTERVAL_SECONDS)
        result['polled'] = True
        return result
    finally:
        cache.delete(lock_key)


def check_single_access_point(ap):
    """On-demand recheck for one AP (UI 'refresh' button)."""
    bridge_macs, arp_by_mac = _fetch_router_link_state(ap.router)
    if bridge_macs is None:
        return ap
    now = timezone.now()
    seen = _resolve_seen(ap, bridge_macs, arp_by_mac)
    ap.status = 'online' if seen else 'offline'
    ap.last_checked = now
    if seen:
        ap.last_seen = now
    ap.save(update_fields=['status', 'last_seen', 'last_checked'])
    return ap