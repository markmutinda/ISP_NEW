import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone
from django_tenants.utils import get_tenant_model, schema_context

logger = logging.getLogger(__name__)


@shared_task(bind=True, queue='default', max_retries=3)
def sync_cpe_device_task(self, device_id: int, schema_name: str):
    """Pull full parameter tree from GenieACS after an Inform. Queued by the public webhook."""
    try:
        with schema_context(schema_name):
            from .models import CPEDevice
            from .services.provisioning import sync_device_from_genieacs

            device = CPEDevice.objects.filter(pk=device_id).first()
            if device:
                sync_device_from_genieacs(device)
    except Exception as exc:
        logger.error("[TR069 SYNC] device=%s schema=%s failed: %s", device_id, schema_name, exc)
        raise self.retry(exc=exc, countdown=15)


@shared_task(queue='default')
def reconcile_all_tenants():
    """
    Runs every 2 minutes. Flips devices that have gone quiet into
    'not_answering' — mirrors how Router.sync_status ages out stale MikroTiks.
    """
    from datetime import timedelta

    threshold = getattr(settings, 'TR069_OFFLINE_AFTER_SECONDS', 900)
    cutoff = timezone.now() - timedelta(seconds=threshold)

    flipped = 0
    from apps.core.models import Tr069DeviceIndex
    from django_tenants.utils import get_public_schema_name

    with schema_context(get_public_schema_name()):
        schemas = list(
            Tr069DeviceIndex.objects.filter(is_active=True)
            .values_list('tenant_schema', flat=True).distinct()
        )

    for schema in schemas:
        try:
            with schema_context(schema):
                from .models import CPEDevice

                flipped += CPEDevice.objects.filter(
                    status='online', last_inform_at__lt=cutoff
                ).update(status='not_answering')
        except Exception as e:
            logger.error("[TR069 RECONCILE] tenant=%s error=%s", schema, e)

    logger.info("[TR069 RECONCILE] flipped %s device(s) to not_answering", flipped)
    return {'flipped': flipped}