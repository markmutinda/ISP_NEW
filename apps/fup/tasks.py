import logging

from celery import shared_task
from django.core.cache import cache
from django_tenants.utils import schema_context, get_tenant_model

logger = logging.getLogger(__name__)


@shared_task(name='apps.fup.tasks.fup_enforce_all')
def fup_enforce_all():
    """Cheap dispatcher: only tenants that actually have an active FUP policy get a task."""
    from apps.fup.models import FUPPolicy

    schemas = (get_tenant_model().objects
               .exclude(schema_name='public').filter(is_active=True)
               .values_list('schema_name', flat=True))
    dispatched = 0
    for schema in schemas:
        try:
            with schema_context(schema):
                if not FUPPolicy.objects.filter(is_active=True, status='ACTIVE').exists():
                    continue
        except Exception:
            continue
        fup_enforce_tenant.apply_async(args=[schema], expires=240)
        dispatched += 1
    return {'dispatched': dispatched}


@shared_task(name='apps.fup.tasks.fup_enforce_tenant', soft_time_limit=240)
def fup_enforce_tenant(schema_name: str):
    from apps.fup.services.engine import FUPEngine

    lock = f'fup:lock:{schema_name}'
    if not cache.add(lock, '1', timeout=280):
        return {'skipped': True}
    try:
        with schema_context(schema_name):
            return FUPEngine().run()
    except Exception:
        logger.exception('[FUP] tenant %s failed', schema_name)
    finally:
        cache.delete(lock)