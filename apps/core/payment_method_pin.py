"""Session-scoped tenant PIN protection for payment gateway settings."""

from hashlib import sha256

from django.core.cache import cache
from django.core import signing
from django.db import connection
from django_tenants.utils import get_public_schema_name, schema_context

from apps.core.models import GlobalSystemSettings


PIN_LENGTH = 6
MAX_FAILED_ATTEMPTS = 5
LOCK_SECONDS = 15 * 60
UNLOCK_SECONDS = 15 * 60
UNLOCK_HEADER = "HTTP_X_PAYMENT_METHODS_UNLOCK"
UNLOCK_SALT = "netily.payment-methods.unlock"


IMPERSONATION_CLAIM = "impersonated_by"


PLATFORM_OPERATOR_CACHE_SECONDS = 5 * 60


def _is_public_platform_superuser(email: str) -> bool:
    """True when this email belongs to an active platform superuser in the public schema."""
    cache_key = f"payment-method-pin:platform-operator:{sha256(email.encode('utf-8')).hexdigest()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return bool(cached)
    from apps.core.models import User

    try:
        with schema_context(get_public_schema_name()):
            result = User.objects.filter(email__iexact=email, is_superuser=True, is_active=True).exists()
    except Exception:
        # Fail closed: an unreachable lookup must not unlock tenant payment settings.
        return False
    cache.set(cache_key, result, timeout=PLATFORM_OPERATOR_CACHE_SECONDS)
    return result


def is_payment_method_pin_bypass_user(user) -> bool:
    """
    Platform operators must not be locked behind a tenant-owned secret.

    `is_superuser` alone is NOT a platform signal: tenant signup grants the
    owning admin is_superuser=True inside their own schema, so treating it as
    platform access let every tenant owner skip their own PIN. A user is a
    platform operator only when their role or configured identity says so, or
    when the same email is an active superuser in the public (platform) schema
    — which is how platform admins are mirrored into tenant schemas.
    """
    if user is None:
        return False
    role = str(getattr(user, "role", "") or "").strip().lower()
    if role in {"superadmin", "super_admin"}:
        return True

    from apps.core.models import AuditLog

    if AuditLog._is_platform_superadmin_actor(user):
        return True
    if not getattr(user, "is_superuser", False):
        return False
    if connection.schema_name == get_public_schema_name():
        return True
    email = str(getattr(user, "email", "") or "").strip().lower()
    return bool(email) and _is_public_platform_superuser(email)


def _token_claim(request, claim: str):
    token = getattr(request, "auth", None)
    try:
        return token.get(claim)
    except AttributeError:
        return None


def is_payment_method_pin_bypass(request) -> bool:
    """
    Superadmins bypass the tenant PIN, including when they open a tenant panel
    through impersonation (the token is issued for the tenant admin but carries
    the operator's id in the impersonated_by claim).
    """
    return bool(
        is_payment_method_pin_bypass_user(getattr(request, "user", None))
        or _token_claim(request, IMPERSONATION_CLAIM)
    )


def _token_id(request) -> str:
    return str(_token_claim(request, "jti") or "no-jti")


def _scope(request) -> str:
    raw = f"{connection.schema_name}:{request.user.pk}:{_token_id(request)}"
    return sha256(raw.encode("utf-8")).hexdigest()


def _pin_version(pin_hash: str) -> str:
    # The salted hash changes on every set/change, so binding unlocks to it
    # relocks every session the moment the PIN is replaced.
    return sha256(str(pin_hash or "").encode("utf-8")).hexdigest()[:16]


def _unlock_identity(request, pin_hash: str) -> str:
    raw = f"{connection.schema_name}:{request.user.pk}:{_pin_version(pin_hash)}"
    return sha256(raw.encode("utf-8")).hexdigest()


def _cache_key(kind: str, request) -> str:
    return f"payment-method-pin:{kind}:{_scope(request)}"


def _unlock_cache_key(request, pin_hash: str) -> str:
    return f"{_cache_key('unlocked', request)}:{_pin_version(pin_hash)}"


def _current_pin_hash() -> str:
    return GlobalSystemSettings.get_solo().payment_methods_pin_hash or ""


def is_pin_configured() -> bool:
    return bool(_current_pin_hash())


def payment_method_pin_is_unlocked(request) -> bool:
    if is_payment_method_pin_bypass(request):
        return True
    pin_hash = _current_pin_hash()
    if not pin_hash:
        return False
    token = str(request.META.get(UNLOCK_HEADER) or "").strip()
    if token:
        try:
            payload = signing.loads(token, salt=UNLOCK_SALT, max_age=UNLOCK_SECONDS)
            if payload == _unlock_identity(request, pin_hash):
                return True
        except (signing.BadSignature, signing.SignatureExpired):
            pass
    return bool(cache.get(_unlock_cache_key(request, pin_hash)))


def unlock_payment_methods(request) -> str:
    pin_hash = _current_pin_hash()
    cache.set(_unlock_cache_key(request, pin_hash), True, timeout=UNLOCK_SECONDS)
    cache.delete(_cache_key("failures", request))
    cache.delete(_cache_key("locked", request))
    return signing.dumps(_unlock_identity(request, pin_hash), salt=UNLOCK_SALT, compress=True)


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
