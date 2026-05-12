from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.db.utils import ProgrammingError, OperationalError
from django.db import transaction
from apps.core.models import Tenant
from apps.radius.models import RadiusTenantConfig
from apps.radius.services.tenant_radius_service import tenant_radius_service
import logging

logger = logging.getLogger(__name__)

@receiver(post_save, sender=Tenant)
def auto_provision_radius_config(sender, instance, created, **kwargs):
    """
    When a new ISP creates an account, we ONLY create their DB config.
    No Docker containers are started.
    """
    if not created:
        return

    display_name = getattr(instance, 'name', getattr(instance, 'company_name', instance.schema_name))

    def _provision():
        """
        Deferred provisioning function that runs after the transaction commits.
        This ensures the tenant schema and tables exist before we try to access them.
        """
        try:
            # 1. Create the Radius config entry in the public schema.
            RadiusTenantConfig.objects.get_or_create(
                schema_name=instance.schema_name,
                defaults={
                    'tenant_name': display_name,
                    'is_active': True,
                }
            )

            # 2. Generate the directory structure and queries.conf.
            # The shared RADIUS server will use these files dynamically.
            tenant_radius_service.configure_tenant_radius(
                schema_name=instance.schema_name,
                tenant_name=display_name
            )

            logger.info("✅ [ISP PROVISIONING] RADIUS database records initialized for %s.", display_name)
        except (ProgrammingError, OperationalError) as e:
            # Table may not exist yet in fresh tenant schema — safe to ignore
            logger.warning(
                "RADIUS provisioning skipped for tenant '%s' (table not ready): %s",
                instance.schema_name, e,
            )
        except Exception:
            logger.exception(
                "RADIUS provisioning failed for tenant '%s' (schema: %s). "
                "Tenant registration will continue.",
                display_name,
                instance.schema_name,
            )

    # Defer until after the current transaction commits so the schema/tables exist
    transaction.on_commit(_provision)


@receiver(post_delete, sender=Tenant)
def auto_cleanup_radius_config(sender, instance, **kwargs):
    """
    Clean up DB records and files, but NEVER restart Docker containers.
    """
    schema_name = instance.schema_name
    
    # Wrap the RadiusTenantConfig deletion in a try/except block
    # This handles the case where the schema or table is already dropped
    try:
        # 1. Remove the config from the public registry
        RadiusTenantConfig.objects.filter(schema_name=schema_name).delete()
    except ProgrammingError:
        # The schema or table was already dropped by Django Tenants. Safe to ignore.
        pass
    
    # 2. Physically delete the tenant's config folder and .env
    # We use the existing service method but we will gut the Docker parts of it next.
    # Wrap this in a try/except as well to handle missing directories gracefully
    try:
        tenant_radius_service.remove_tenant_radius(schema_name)
        print(f"✅ [ISP CLEANUP] RADIUS records and files removed for {schema_name}.")
    except Exception as e:
        # Log but don't crash - files might already be deleted
        logger.warning(f"⚠️ [ISP CLEANUP] Could not remove RADIUS files for {schema_name}: {e}")