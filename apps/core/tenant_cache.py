# apps/core/tenant_cache.py
from django.core.cache import cache
from django_tenants.utils import schema_context, get_public_schema_name

TENANT_CACHE_TTL = 600          # 10 min — invalidated instantly on tenant writes
TENANT_NEGATIVE_TTL = 30        # short TTL for "not found" to absorb bad/typo hosts
_PREFIX = "tenant_resolve:v1:"


def _key(identifier: str) -> str:
    return f"{_PREFIX}{identifier.lower()}"


def resolve_tenant_cached(identifier: str):
    """Resolve tenant by subdomain OR schema_name. Returns {id, schema_name, subdomain} or None."""
    if not identifier:
        return None

    key = _key(identifier)
    cached = cache.get(key)
    if cached is not None:
        return cached or None  # False sentinel == cached "not found"

    from django.db.models import Q
    from apps.core.models import Tenant

    with schema_context(get_public_schema_name()):
        tenant = (
            Tenant.objects.filter(
                Q(subdomain=identifier) | Q(schema_name=identifier),
                is_active=True,
            )
            .only("id", "schema_name", "subdomain")
            .first()
        )

    if not tenant:
        cache.set(key, False, timeout=TENANT_NEGATIVE_TTL)
        return None

    payload = {"id": tenant.id, "schema_name": tenant.schema_name, "subdomain": tenant.subdomain}
    cache.set(key, payload, timeout=TENANT_CACHE_TTL)
    return payload


def invalidate_tenant_cache(subdomain: str = None, schema_name: str = None):
    if subdomain:
        cache.delete(_key(subdomain))
    if schema_name:
        cache.delete(_key(schema_name))