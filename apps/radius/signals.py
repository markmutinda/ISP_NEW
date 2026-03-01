from django.db.models.signals import post_save, post_delete  # Added post_delete
from django.dispatch import receiver
from apps.core.models import Tenant  # Update this import if your Tenant model is elsewhere
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
        
        # --- THIS IS THE CHANGE ---
        # Safely get the name. If 'name' doesn't exist, try 'company_name', else use 'schema_name'
        display_name = getattr(instance, 'name', getattr(instance, 'company_name', instance.schema_name))
        # --------------------------

        # 1. Create the Radius config (models.py will auto-assign unique 18xx ports)
        RadiusTenantConfig.objects.get_or_create(
            schema_name=instance.schema_name,
            defaults={
                'tenant_name': display_name,  # <--- Changed here
                'is_active': True
            }
        )
        
        # 2. Generate the .env file and override.yml
        tenant_radius_service.configure_tenant_radius(
            schema_name=instance.schema_name, 
            tenant_name=display_name      # <--- Changed here
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
            print(f"✅ [ISP PROVISIONING] RADIUS container for {display_name} is starting!") # <--- Changed here
        except Exception as e:
            logger.error(f"Failed to start docker container: {e}")


@receiver(post_delete, sender=Tenant)
def auto_cleanup_radius_container(sender, instance, **kwargs):
    """
    When an ISP is deleted from the Superadmin, clean up all Docker resources.
    """
    schema_name = instance.schema_name
    display_name = getattr(instance, 'name', getattr(instance, 'company_name', schema_name))
    
    print(f"\n🗑️ [ISP CLEANUP] Starting teardown for {display_name} ({schema_name})...")

    # 1. Free up the ports in the public registry
    RadiusTenantConfig.objects.filter(schema_name=schema_name).delete()
    print(f" - Released RADIUS ports for {schema_name}.")

    # 2. Delete the specific .env file
    env_file_path = tenant_radius_service.docker_path / f".env.radius.{schema_name}"
    try:
        if env_file_path.exists():
            env_file_path.unlink()
            print(f" - Deleted environment file: {env_file_path.name}")
    except Exception as e:
        logger.error(f"Failed to delete .env file: {e}")

    # 3. Regenerate the override.yml so Docker forgets this tenant
    tenant_radius_service.generate_docker_compose_override()
    print(" - Regenerated docker-compose.override.yml without this tenant.")

    # 4. Tell Docker to apply changes and remove the "orphaned" container
    try:
        # This just stops the orphaned container without trying to "recreate" everything else
        subprocess.run(
            ['docker-compose', 'up', '-d', '--remove-orphans'], 
            cwd=str(tenant_radius_service.docker_path),
            check=False
        ) 
        print(f"✅ [CLEANUP] Orphaned containers removed.")
    except Exception as e:
        logger.error(f"Cleanup command failed: {e}")