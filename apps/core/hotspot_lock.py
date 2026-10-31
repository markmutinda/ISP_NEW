"""
Blocks hotspot (captive portal) purchases/reconnects for tenants whose
Netily subscription is overdue. Fail-open on any error.
"""
import functools
import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework import status
from rest_framework.response import Response

logger = logging.getLogger(__name__)

LOCK_CACHE_TTL = 60                      # seconds
TRIAL_LAPSE_GRACE = timedelta(days=1)    # trial must be 1 day overdue
_KEY = "hotspot_lock:v1:{}"


def invalidate_hotspot_lock(schema_name):
    cache.delete(_KEY.format(schema_name))


def _compute(schema_name):
    from apps.subscriptions.models import CompanySubscription

    with schema_context(get_public_schema_name()):
        row = (
            CompanySubscription.objects
            .filter(company__tenant__schema_name=schema_name)
            .values(
                "status", "current_period_end", "trial_ends_at",
                "company__phone_number", "company__name",
            )
            .first()
        )
    if not row:
        return {"locked": False}

    now = timezone.now()
    st = row["status"]
    end = row["current_period_end"]
    locked = False
    if st in ("past_due", "expired"):
        locked = bool(end and end <= now)
    elif st == "trialing":
        t_end = row["trial_ends_at"]
        locked = bool(t_end and t_end + TRIAL_LAPSE_GRACE <= now)

    return {
        "locked": locked,
        "phone": row["company__phone_number"] or "",
        "name": row["company__name"] or "",
    }


def get_hotspot_lock(schema_name):
    key = _KEY.format(schema_name)
    data = cache.get(key)
    if data is not None:
        return data
    try:
        data = _compute(schema_name)
    except Exception:
        logger.exception("hotspot lock check failed for %s (fail-open)", schema_name)
        return {"locked": False}
    cache.set(key, data, LOCK_CACHE_TTL)
    return data


def _message(lock):
    who = lock.get("name") or "your internet provider"
    return (
        f"Internet access is temporarily unavailable. "
        f"Please contact {who} for assistance."
    )


def locked_payload(schema_name, lock, router_id=None):
    """Router-specific support phone if available, else company phone."""
    phone = lock.get("phone", "")
    hotspot_name = lock.get("name", "")
    if router_id:
        try:
            from apps.network.models.router_models import Router
            with schema_context(schema_name):
                r = Router.objects.filter(pk=router_id).values(
                    "support_phone", "hotspot_name", "name"
                ).first()
            if r:
                phone = r["support_phone"] or phone
                hotspot_name = r["hotspot_name"] or r["name"] or hotspot_name
        except Exception:
            pass
    return {
        "status": "suspended",
        "suspended": True,
        "hotspot_suspended": True,
        "message": _message(lock),
        "portal_config": {
            "template_id": 1,
            "hotspot_name": hotspot_name,
            "support_phone": phone,
            "announcement_text": "",
            "gateway_ip": "",
        },
        "branding": None,
        "plans": [],
    }


def hotspot_subscription_guard(view_method):
    """Decorator for public hotspot APIView methods (reads `tenant` from body/query)."""
    @functools.wraps(view_method)
    def wrapper(self, request, *args, **kwargs):
        try:
            ref = request.data.get("tenant") or request.query_params.get("tenant")
            if ref:
                from apps.core.tenant_cache import resolve_tenant_cached
                info = resolve_tenant_cached(ref)
                if info:
                    lock = get_hotspot_lock(info["schema_name"])
                    if lock["locked"]:
                        router_id = request.data.get("router_id") or request.query_params.get("router_id")
                        payload = locked_payload(info["schema_name"], lock, router_id)
                        return Response(
                            {
                                "error": payload["message"],
                                "code": "HOTSPOT_SUSPENDED",
                                "hotspot_suspended": True,
                                "support_phone": payload["portal_config"]["support_phone"],
                            },
                            status=status.HTTP_403_FORBIDDEN,
                        )
        except Exception:
            logger.exception("hotspot guard error (fail-open)")
        return view_method(self, request, *args, **kwargs)
    return wrapper