"""
Shared helpers for hotspot device credentials.

Slot 1 = the client's canonical username (e.g. MXA-BKCS).
Slots 2..N = "<canonical>-<n>", matching the naming already used by
HotspotPurchaseView / HotspotPhoneReconnectView.
"""
import re
from typing import Dict, List

from django.core.cache import cache
from django.db import transaction

BASE_CODE_RE = re.compile(r'^[A-Z0-9]{4}-[A-Z0-9]{4}$')
SLOT_CODE_RE = re.compile(r'^(?P<base>[A-Z0-9]{4}-[A-Z0-9]{4})-(?P<slot>\d{1,2})$')
MAX_DEVICE_SLOTS = 10  # hard cap: protects SMS length and abuse


def device_password(username: str) -> str:
    """Single source of truth for the hotspot password (currently == username)."""
    return username


def device_limit(plan) -> int:
    try:
        limit = int(getattr(plan, 'simultaneous_devices', 1) or 1)
    except (TypeError, ValueError):
        limit = 1
    return max(1, min(limit, MAX_DEVICE_SLOTS))


def build_device_credentials(session) -> List[Dict]:
    """
    Returns [{'slot': 1, 'username': ..., 'password': ...}, ...].
    Multi-device plans get one login per allowed device, but only when the
    session holds the canonical (slot 1) code; otherwise just its own login.
    """
    code = (session.access_code or '').strip()
    limit = device_limit(session.plan)

    if limit == 1 or not BASE_CODE_RE.match(code.upper()):
        return [{'slot': 1, 'username': code, 'password': device_password(code)}]

    base = code.upper()
    out = []
    for n in range(1, limit + 1):
        user = base if n == 1 else f"{base}-{n}"
        out.append({'slot': n, 'username': user, 'password': device_password(user)})
    return out


# ═══════════════════════════════════════════════════════════════════════════
# TV PUSH-LOGIN SCHEDULING — server-side hotspot login for Smart TVs
# ═══════════════════════════════════════════════════════════════════════════

def schedule_tv_login_push(session, schema_name: str) -> None:
    """Queue a server-side login if this purchase was flagged as 'pay for TV'."""
    if not cache.get(f"hs_tv_push:{schema_name}:{session.session_id}"):
        return
    # Webhook + status poll can both reach activation; queue only once
    if not cache.add(f"hs_tv_push_queued:{schema_name}:{session.session_id}", 1, timeout=60):
        return

    def _queue():
        from apps.billing.tasks import push_hotspot_login
        push_hotspot_login.apply_async(args=[session.session_id, schema_name], countdown=2)

    transaction.on_commit(_queue)