"""Session-scoped tenant PIN protection for payment gateway settings."""

from hashlib import sha256

from django.core.cache import cache
from django.db import connection

from apps.core.models import GlobalSystemSettings


PIN_LENGTH = 6
MAX_FAILED_ATTEMPTS = 5
LOCK_SECONDS = 15 * 60
UNLOCK_SECONDS = 15 * 60


def _token_id(request) -> str:
    token = getattr(request, "auth", None)
    try:
        value = token.get("jti", "")
    except AttributeError:
        value = ""
    return str(value or "no-jti")


def _scope(request) -> str:
    raw = f"{connection.schema_name}:{request.user.pk}:{_token_id(request)}"
    return sha256(raw.encode("utf-8")).hexdigest()


def _cache_key(kind: str, request) -> str:
    return f"payment-method-pin:{kind}:{_scope(request)}"


def is_pin_configured() -> bool:
    return bool(GlobalSystemSettings.get_solo().payment_methods_pin_hash)


def payment_method_pin_is_unlocked(request) -> bool:
    if getattr(request.user, "is_superuser", False):
        return True
    if not is_pin_configured():
        return False
    return bool(cache.get(_cache_key("unlocked", request)))


def unlock_payment_methods(request) -> None:
    cache.set(_cache_key("unlocked", request), True, timeout=UNLOCK_SECONDS)
    cache.delete(_cache_key("failures", request))
    cache.delete(_cache_key("locked", request))


def lock_seconds_remaining(request) -> int:
    # The cache backend does not expose a portable TTL API, so keep an expiry
    # timestamp as the value and calculate a useful countdown for the UI.
    import time

    expires_at = cache.get(_cache_key("locked", request))
    if not expires_at:
        return 0
    return max(0, int(float(expires_at) - time.time()))


def record_failed_attempt(request) -> int:
    import time

    key = _cache_key("failures", request)
    attempts = int(cache.get(key) or 0) + 1
    cache.set(key, attempts, timeout=LOCK_SECONDS)
    if attempts >= MAX_FAILED_ATTEMPTS:
        cache.set(_cache_key("locked", request), time.time() + LOCK_SECONDS, timeout=LOCK_SECONDS)
    return attempts


def validate_new_pin(pin: str) -> str:
    value = str(pin or "").strip()
    if not (value.isdigit() and len(value) == PIN_LENGTH):
        raise ValueError("Use exactly 6 digits for the Payment Methods PIN.")
    if len(set(value)) == 1 or value in {"123456", "654321"}:
        raise ValueError("Choose a less predictable 6-digit PIN.")
    return value
