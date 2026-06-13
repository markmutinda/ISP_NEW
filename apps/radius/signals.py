import logging

from django.db import DatabaseError, transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.core.models import Tenant
from apps.radius.models import RadiusTenantConfig
from apps.radius.services.tenant_radius_service import tenant_radius_service

logger = logging.getLogger(__name__)


def _cleanup_radius_registry(schema_name: str) -> None:
    """
    Best-effort tenant RADIUS cleanup.

    Running this after commit prevents a swallowed database error from
    poisoning the surrounding tenant-deletion transaction.
    """
    try:
        RadiusTenantConfig.objects.filter(schema_name=schema_name).delete()
    except DatabaseError as exc:
        logger.warning(
            "[ISP CLEANUP] Could not remove RADIUS registry for %s: %s",
            schema_name,
            exc,
        )

    try:
        tenant_radius_service.remove_tenant_radius(schema_name)
        logger.info("[ISP CLEANUP] RADIUS records removed for %s.", schema_name)
    except Exception as exc:
        logger.warning(
            "[ISP CLEANUP] Could not remove RADIUS files for %s: %s",
            schema_name,
            exc,
        )


@receiver(post_save, sender=Tenant)
def auto_provision_radius_config(sender, instance, created, **kwargs):
    """
    Tenant RADIUS onboarding is triggered explicitly after tenant schema
    migration succeeds. Avoid provisioning on Tenant post_save because that
    races with schema creation during registration.
    """
    return


@receiver(post_delete, sender=Tenant)
def auto_cleanup_radius_config(sender, instance, **kwargs):
    """
    Clean up RADIUS registry data after the surrounding delete transaction
    commits successfully.
    """
    schema_name = instance.schema_name

    if transaction.get_connection().in_atomic_block:
        transaction.on_commit(lambda: _cleanup_radius_registry(schema_name))
        return

    _cleanup_radius_registry(schema_name)
