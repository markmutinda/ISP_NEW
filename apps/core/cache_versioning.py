# apps/core/cache_versioning.py
from django.core.cache import cache

def _ver_key(schema_name: str) -> str:
    return f"hotspot:captive:ver:{schema_name}"

def get_cache_version(schema_name: str) -> int:
    v = cache.get(_ver_key(schema_name))
    if v is None:
        v = 1
        cache.set(_ver_key(schema_name), v, timeout=None)
    return v

def bump_cache_version(schema_name: str) -> None:
    key = _ver_key(schema_name)
    try:
        cache.incr(key)
    except ValueError:
        cache.set(key, 2, timeout=None)