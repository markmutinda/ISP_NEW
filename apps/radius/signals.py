from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from apps.core.models import Tenant
from apps.radius.models import RadiusTenantConfig
from apps.radius.services.tenant_radius_service import tenant_radius_service
import subprocess
import logging

logger = logging.getLogger(__name__)

@receiver(post_save, sender=Tenant)
def auto_provision_radius_container(sender, instance, created, **kwargs):
    """
    When a new ISP creates an account, automatically build their RADIUS container.
    """
    if created:
        print(f"🚀 [ISP PROVISIONING] Building RADIUS for {instance.schema_name}...")
        
        # Safely get the name. If 'name' doesn't exist, try 'company_name', else use 'schema_name'
        display_name = getattr(instance, 'name', getattr(instance, 'company_name', instance.schema_name))

        # 1. Create the Radius config (models.py will auto-assign unique 18xx ports)
        RadiusTenantConfig.objects.get_or_create(
            schema_name=instance.schema_name,
            defaults={
                'tenant_name': display_name,
                'is_active': True
            }
        )
        
        # 2. Generate the .env file and override.yml
        tenant_radius_service.configure_tenant_radius(
            schema_name=instance.schema_name, 
            tenant_name=display_name
        )
        tenant_radius_service.generate_docker_compose_override()
        
        # 3. Ask Docker to spin up the new container in the background
        try:
            # "docker-compose up -d" will leave existing containers running, 
            # and only build/start the newly added one!
            subprocess.Popen(
                ['docker-compose', 'up', '-d'],
                cwd=str(tenant_radius_service.docker_path)
            )
            print(f"✅ [ISP PROVISIONING] RADIUS container for {display_name} is starting!")
        except Exception as e:
            logger.error(f"Failed to start docker container: {e}")


@receiver(post_delete, sender=Tenant)
def auto_cleanup_radius_container(sender, instance, **kwargs):
    """
    When an ISP is deleted from the Superadmin, clean up all Docker resources
    using targeted CLI commands to prevent stack-wide downtime.
    """
    schema_name = instance.schema_name
    display_name = getattr(instance, 'name', getattr(instance, 'company_name', schema_name))
    
    print(f"\n🗑️ [ISP CLEANUP] Starting zero-downtime teardown for {display_name}...")

    # 1. Free up the ports in the public registry
    RadiusTenantConfig.objects.filter(schema_name=schema_name).delete()
    print(f" - Released RADIUS ports for {schema_name}.")

    # 2. Hand off the actual Docker/File cleanup to the service
    # This targets ONLY the deleted tenant's container and avoids restarting the backend.
    success = tenant_radius_service.remove_tenant_radius(schema_name)
    
    if success:
        print(f"✅ [ISP CLEANUP] Successfully removed all artifacts for {schema_name}.")
    else:
        logger.error(f"Failed to cleanly remove Docker artifacts for {schema_name}.")