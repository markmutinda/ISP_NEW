from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from apps.core.models import Tenant
from apps.radius.models import RadiusTenantConfig
from apps.radius.tasks import provision_tenant_infrastructure_task, teardown_tenant_infrastructure_task
import logging

logger = logging.getLogger(__name__)

@receiver(post_save, sender=Tenant)
def auto_provision_radius_container(sender, instance, created, **kwargs):
    """
    When a new ISP creates an account, automatically build their RADIUS container.
    """
    if created:
        display_name = getattr(instance, 'name', getattr(instance, 'company_name', instance.schema_name))
        print(f"🚀 [ISP PROVISIONING] Creating RADIUS config for {instance.schema_name}...")
        
        # 1. Create the Radius config (models.py will auto-assign unique 18xx ports)
        # This is fast and stays in the web process
        RadiusTenantConfig.objects.get_or_create(
            schema_name=instance.schema_name,
            defaults={
                'tenant_name': display_name,
                'is_active': True
            }
        )
        
        # 2. Generate the queries.conf and basic config files
        # This is also fast - just file writes
        from apps.radius.services.tenant_radius_service import tenant_radius_service
        tenant_radius_service.configure_tenant_radius(
            schema_name=instance.schema_name, 
            tenant_name=display_name
        )
        
        # 3. Hand off the heavy lifting (Docker build, container start, VPN injection) to Celery
        print(f"📦 [SIGNAL] Handing off infrastructure build for {instance.schema_name} to Celery...")
        provision_tenant_infrastructure_task.delay(instance.schema_name)


@receiver(post_delete, sender=Tenant)
def auto_cleanup_radius_container(sender, instance, **kwargs):
    """
    When an ISP is deleted from the Superadmin, clean up all Docker resources 
    without restarting the backend.
    """
    schema_name = instance.schema_name
    display_name = getattr(instance, 'name', getattr(instance, 'company_name', schema_name))
    
    print(f"\n🗑️ [ISP CLEANUP] Starting teardown for {display_name} ({schema_name})...")

    # 1. Free up the ports in the public registry (fast DB operation)
    RadiusTenantConfig.objects.filter(schema_name=schema_name).delete()
    print(f" - Released RADIUS ports for {schema_name}.")

    # 2. Hand off the heavy lifting (Docker stop/rm, VPN cleanup) to Celery
    print(f"📦 [SIGNAL] Handing off infrastructure teardown for {schema_name} to Celery...")
    teardown_tenant_infrastructure_task.delay(schema_name)