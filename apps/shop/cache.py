from django.core.cache import cache

_VER_KEY = "shop:catalog:ver"


def get_catalog_version() -> int:
    v = cache.get(_VER_KEY)
    if v is None:
        cache.add(_VER_KEY, 1, timeout=None)
        v = cache.get(_VER_KEY) or 1
    return v


def bump_catalog_version() -> None:
    try:
        cache.incr(_VER_KEY)
    except ValueError:
        cache.set(_VER_KEY, 2, timeout=None)