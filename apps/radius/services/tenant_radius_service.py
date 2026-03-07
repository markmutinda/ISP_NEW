"""
Tenant RADIUS Configuration Service (SHARED ARCHITECTURE)

This service handles the logic for a single, shared RADIUS cluster:
1. Manages directory structures for tenant-specific SQL queries.
2. Registers tenants in the public registry for the central RADIUS 'Brain'.
3. Cleans up files during tenant deletion without touching Docker infrastructure.
"""

import os
import logging
import shutil
from pathlib import Path
from typing import Optional, Dict, Any
from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)


class TenantRadiusConfigService:
    """
    Service for managing a Centralized Shared RADIUS environment.
    No longer creates or deletes individual Docker containers.
    """
    
    def __init__(self):
        self.base_path = Path(settings.BASE_DIR)
        self.radius_config_path = self.base_path / 'radius_config'
        self.docker_path = self.base_path / 'docker'
        
    def get_current_tenant_schema(self) -> str:
        """Get the current tenant schema name from Django connection."""
        try:
            return connection.schema_name
        except AttributeError:
            # Fallback for non-tenant context
            return 'public'
        
    def configure_tenant_radius(
        self,
        schema_name: str,
        tenant_name: str = None,
        regenerate: bool = False
    ) -> Dict[str, Any]:
        """
        Initializes the database records and folder structure for a tenant.
        Does NOT touch Docker.
        """
        if not schema_name or schema_name == 'public':
            raise ValueError("Cannot configure RADIUS for public schema")
        
        tenant_name = tenant_name or schema_name.replace('tenant_', '').title()
        
        # 1. Create tenant-specific config directory for future SQL logs/overrides
        tenant_config_dir = self.radius_config_path / 'tenants' / schema_name
        tenant_config_dir.mkdir(parents=True, exist_ok=True)
        
        # 2. Update tenant registry in public schema (Force SHARED mode)
        from ..models import RadiusTenantConfig
        RadiusTenantConfig.objects.update_or_create(
            schema_name=schema_name,
            defaults={
                'tenant_name': tenant_name,
                'is_active': True,
            }
        )
        
        logger.info(f"✅ RADIUS database records initialized for tenant: {schema_name}")
        return {'schema_name': schema_name, 'status': 'ready'}

    def remove_tenant_radius(self, schema_name: str) -> bool:
        """
        Cleans up files only. Never stops or removes containers.
        """
        try:
            # 1. Delete the tenant-specific .env file if it exists (Cleanup from old architecture)
            env_path = self.docker_path / f'.env.radius.{schema_name}'
            if env_path.exists():
                os.remove(env_path)
                logger.info(f"Removed env file: {env_path}")

            # 2. Delete the config folder
            tenant_config_dir = self.radius_config_path / 'tenants' / schema_name
            if tenant_config_dir.exists():
                shutil.rmtree(tenant_config_dir)
                logger.info(f"Removed config directory: {tenant_config_dir}")
                
            logger.info(f"✅ File cleanup complete for {schema_name}.")
            return True
        except Exception as e:
            logger.error(f"Failed to cleanup files for {schema_name}: {e}")
            return False

    def restart_shared_radius(self) -> bool:
        """
        Restarts the single, central RADIUS container to refresh NAS clients.
        """
        import subprocess
        try:
            subprocess.run(
                ['docker', 'restart', 'netily_radius'],
                capture_output=True, check=True
            )
            logger.info("✅ Central RADIUS container restarted successfully")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to restart central RADIUS: {e.stderr.decode() if e.stderr else str(e)}")
            return False
        except Exception as e:
            logger.error(f"Failed to restart central RADIUS: {e}")
            return False


# Singleton instance
tenant_radius_service = TenantRadiusConfigService()