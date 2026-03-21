from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
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
    if created:
        display_name = getattr(instance, 'name', getattr(instance, 'company_name', instance.schema_name))

        # Keep registration flow resilient: RADIUS bootstrap should never block tenant creation.
        try:
            # 1. Create the Radius config entry in the public schema.
            # NOTE: deployment_mode was removed from the model, so we only pass valid fields.
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
        except Exception:
            logger.exception(
                "RADIUS provisioning failed for tenant '%s' (schema: %s). "
                "Tenant registration will continue.",
                display_name,
                instance.schema_name,
            )


@receiver(post_delete, sender=Tenant)
def auto_cleanup_radius_config(sender, instance, **kwargs):
    """
    Clean up DB records and files, but NEVER restart Docker containers.
    """
    schema_name = instance.schema_name
    
    # 1. Remove the config from the public registry
    RadiusTenantConfig.objects.filter(schema_name=schema_name).delete()
    
    # 2. Physically delete the tenant's config folder and .env
    # We use the existing service method but we will gut the Docker parts of it next.
    tenant_radius_service.remove_tenant_radius(schema_name)
    
    print(f"✅ [ISP CLEANUP] RADIUS records and files removed for {schema_name}.")