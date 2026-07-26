# apps/core/country_utils.py
from django.core.cache import cache
from django_tenants.utils import schema_context, get_public_schema_name


def get_tenant_country_code(schema_name: str) -> str:
    """
    Resolve a tenant's country code (e.g. 'GH') from the public-schema Company
    record, cached briefly since this is called on hot paths (every SMS/payment).
    Falls back to 'KE' if unresolved, matching existing default behavior.
    """
    cache_key = f"tenant_country:{schema_name}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    country_code = 'KE'
    try:
        with schema_context(get_public_schema_name()):
            from apps.core.models import Tenant
            tenant = Tenant.objects.select_related('company').filter(
                schema_name=schema_name
            ).first()
            if tenant and tenant.company and tenant.company.country:
                country_code = tenant.company.country
    except Exception:
        pass  # keep default on any resolution failure

    cache.set(cache_key, country_code, timeout=3600)
    return country_code


def get_tenant_base_currency(schema_name: str) -> str:
    """
    Resolve a tenant's base currency (e.g. 'GHS') from the public-schema
    Company record. Cached alongside country resolution since they're
    fetched together on the same hot paths (payment creation, invoicing).
    Falls back to 'KES' if unresolved.
    """
    cache_key = f"tenant_currency:{schema_name}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    currency = 'KES'
    try:
        with schema_context(get_public_schema_name()):
            from apps.core.models import Tenant
            tenant = Tenant.objects.select_related('company').filter(
                schema_name=schema_name
            ).first()
            if tenant and tenant.company and tenant.company.base_currency:
                currency = tenant.company.base_currency
    except Exception:
        pass  # keep default on any resolution failure

    cache.set(cache_key, currency, timeout=3600)
    return currency


def get_tenant_country_and_currency(schema_name: str) -> tuple:
    """
    Get both country code and base currency for a tenant in a single
    database query, cached together for efficiency.
    
    Returns:
        tuple: (country_code, base_currency) e.g. ('GH', 'GHS')
    """
    cache_key = f"tenant_country_currency:{schema_name}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    country_code = 'KE'
    currency = 'KES'
    
    try:
        with schema_context(get_public_schema_name()):
            from apps.core.models import Tenant
            tenant = Tenant.objects.select_related('company').filter(
                schema_name=schema_name
            ).first()
            if tenant and tenant.company:
                if tenant.company.country:
                    country_code = tenant.company.country
                if tenant.company.base_currency:
                    currency = tenant.company.base_currency
    except Exception:
        pass  # keep defaults on any resolution failure

    result = (country_code, currency)
    cache.set(cache_key, result, timeout=3600)
    return result