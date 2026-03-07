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
        
        # 1. Create the Radius config entry in the public schema
        RadiusTenantConfig.objects.get_or_create(
            schema_name=instance.schema_name,
            defaults={
                'tenant_name': display_name,
                'is_active': True,
                'deployment_mode': 'SHARED' # Force shared mode
            }
        )
        
        # 2. Generate the directory structure and queries.conf
        # The shared RADIUS server will use these files dynamically.
        tenant_radius_service.configure_tenant_radius(
            schema_name=instance.schema_name, 
            tenant_name=display_name
        )
        
        print(f"✅ [ISP PROVISIONING] RADIUS database records initialized for {display_name}.")


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