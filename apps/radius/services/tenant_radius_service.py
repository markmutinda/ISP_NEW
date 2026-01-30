"""
Tenant RADIUS Configuration Service

This service handles multi-tenant RADIUS configuration:
1. Auto-creates RADIUS config when new tenant is registered
2. Manages FreeRADIUS configuration per tenant
3. Provides tenant-aware RADIUS user creation

Architecture (Splynx-inspired):
- Each tenant gets their own schema with RADIUS tables
- FreeRADIUS can query dynamically based on NAS → Tenant mapping
- OR each tenant gets their own RADIUS container (recommended for isolation)
"""

import os
import re
import json
import logging
import subprocess
from typing import Optional, Dict, Any, List
from pathlib import Path
from django.conf import settings
from django.db import connection
from django.core.cache import cache

logger = logging.getLogger(__name__)


class TenantRadiusConfigService:
    """
    Service for managing multi-tenant RADIUS configuration.
    
    Supports two deployment modes:
    1. Shared RADIUS: Single FreeRADIUS with dynamic schema lookup
    2. Isolated RADIUS: Separate FreeRADIUS container per tenant
    """
    
    # Cache keys
    TENANT_CONFIG_CACHE_KEY = "radius_tenant_config_{schema}"
    NAS_TENANT_MAP_CACHE_KEY = "radius_nas_tenant_map"
    
    def __init__(self):
        self.base_path = Path(settings.BASE_DIR)
        self.radius_config_path = self.base_path / 'radius_config'
        self.docker_path = self.base_path / 'docker'
        
    # ────────────────────────────────────────────────────────────────
    # TENANT CONFIGURATION
    # ────────────────────────────────────────────────────────────────
    
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
        Configure RADIUS for a new tenant.
        
        This creates:
        1. Tenant-specific queries.conf with schema-qualified tables
        2. Docker environment file for the tenant
        3. Updates NAS → Tenant mapping
        
        Args:
            schema_name: Tenant schema name (e.g., 'tenant_myisp')
            tenant_name: Human-readable tenant name
            regenerate: Force regeneration even if config exists
            
        Returns:
            Configuration result dict
        """
        if not schema_name or schema_name == 'public':
            raise ValueError("Cannot configure RADIUS for public schema")
        
        tenant_name = tenant_name or schema_name.replace('tenant_', '').title()
        
        result = {
            'schema_name': schema_name,
            'tenant_name': tenant_name,
            'config_created': False,
            'queries_path': None,
            'env_path': None,
        }
        
        # Create tenant-specific config directory
        tenant_config_dir = self.radius_config_path / 'tenants' / schema_name
        tenant_config_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate queries.conf for this tenant
        queries_path = tenant_config_dir / 'queries.conf'
        if not queries_path.exists() or regenerate:
            self._generate_tenant_queries_conf(schema_name, queries_path)
            result['config_created'] = True
        result['queries_path'] = str(queries_path)
        
        # Generate Docker .env file for this tenant
        env_path = self.docker_path / f'.env.radius.{schema_name}'
        if not env_path.exists() or regenerate:
            self._generate_tenant_env_file(schema_name, tenant_name, env_path)
        result['env_path'] = str(env_path)
        
        # Update tenant registry
        self._register_tenant(schema_name, tenant_name)
        
        # Clear cache
        cache.delete(self.TENANT_CONFIG_CACHE_KEY.format(schema=schema_name))
        cache.delete(self.NAS_TENANT_MAP_CACHE_KEY)
        
        logger.info(f"Configured RADIUS for tenant: {schema_name}")
        return result
    
    def _generate_tenant_queries_conf(self, schema_name: str, output_path: Path):
        """Generate schema-qualified queries.conf for a tenant."""
        
        queries_content = f'''# -*- text -*-
##
## FreeRADIUS PostgreSQL Queries - Tenant: {schema_name}
##
## Auto-generated for Netily ISP multi-tenant RADIUS
## Schema: {schema_name}
##

# Safe characters for SQL queries
safe_characters = "@abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_: /+"

#######################################################################
# Authorization Queries - Schema: {schema_name}
#######################################################################

# Get check attributes for a user
authorize_check_query = "\\
    SELECT id, username, attribute, value, op \\
    FROM {schema_name}.radcheck \\
    WHERE username = '%{{SQL-User-Name}}' \\
    ORDER BY id"

# Get reply attributes for a user
authorize_reply_query = "\\
    SELECT id, username, attribute, value, op \\
    FROM {schema_name}.radreply \\
    WHERE username = '%{{SQL-User-Name}}' \\
    ORDER BY id"

# Group queries - using group tables for profile-based policies
authorize_group_check_query = "\\
    SELECT radgroupcheck.id, radgroupcheck.groupname, radgroupcheck.attribute, \\
           radgroupcheck.value, radgroupcheck.op \\
    FROM {schema_name}.radusergroup \\
    JOIN {schema_name}.radgroupcheck ON radusergroup.groupname = radgroupcheck.groupname \\
    WHERE radusergroup.username = '%{{SQL-User-Name}}' \\
    ORDER BY radusergroup.priority, radgroupcheck.id"

authorize_group_reply_query = "\\
    SELECT radgroupreply.id, radgroupreply.groupname, radgroupreply.attribute, \\
           radgroupreply.value, radgroupreply.op \\
    FROM {schema_name}.radusergroup \\
    JOIN {schema_name}.radgroupreply ON radusergroup.groupname = radgroupreply.groupname \\
    WHERE radusergroup.username = '%{{SQL-User-Name}}' \\
    ORDER BY radusergroup.priority, radgroupreply.id"

group_membership_query = "\\
    SELECT groupname \\
    FROM {schema_name}.radusergroup \\
    WHERE username = '%{{SQL-User-Name}}' \\
    ORDER BY priority"

#######################################################################
# Simultaneous Use Checking Queries
#######################################################################

simul_count_query = "\\
    SELECT COUNT(*) \\
    FROM {schema_name}.radacct \\
    WHERE username = '%{{SQL-User-Name}}' \\
    AND acctstoptime IS NULL"

simul_verify_query = "\\
    SELECT radacctid, acctsessionid, username, nasipaddress, nasportid, \\
           framedipaddress, callingstationid, framedprotocol \\
    FROM {schema_name}.radacct \\
    WHERE username = '%{{SQL-User-Name}}' \\
    AND acctstoptime IS NULL"

#######################################################################
# Accounting Queries
#######################################################################

accounting_start_query = "\\
    INSERT INTO {schema_name}.radacct \\
        (acctsessionid, acctuniqueid, username, realm, nasipaddress, \\
         nasportid, nasporttype, acctstarttime, acctupdatetime, \\
         acctstoptime, acctsessiontime, acctauthentic, connectinfo_start, \\
         acctinputoctets, acctoutputoctets, calledstationid, \\
         callingstationid, acctterminatecause, servicetype, framedprotocol, \\
         framedipaddress) \\
    VALUES \\
        ('%{{Acct-Session-Id}}', '%{{Acct-Unique-Session-Id}}', \\
         '%{{SQL-User-Name}}', '%{{Realm}}', '%{{NAS-IP-Address}}', \\
         '%{{NAS-Port-Id}}', '%{{NAS-Port-Type}}', NOW(), NOW(), \\
         NULL, 0, '%{{Acct-Authentic}}', '%{{Connect-Info}}', \\
         0, 0, '%{{Called-Station-Id}}', '%{{Calling-Station-Id}}', \\
         '', '%{{Service-Type}}', '%{{Framed-Protocol}}', \\
         '%{{Framed-IP-Address}}')"

accounting_interim_query = "\\
    UPDATE {schema_name}.radacct \\
    SET acctupdatetime = NOW(), \\
        acctinterval = %{{%{{Acct-Session-Time}}:-0}} - acctsessiontime, \\
        acctsessiontime = %{{%{{Acct-Session-Time}}:-0}}, \\
        acctinputoctets = %{{%{{Acct-Input-Gigawords}}:-0}} * 4294967296 + %{{%{{Acct-Input-Octets}}:-0}}, \\
        acctoutputoctets = %{{%{{Acct-Output-Gigawords}}:-0}} * 4294967296 + %{{%{{Acct-Output-Octets}}:-0}}, \\
        framedipaddress = '%{{Framed-IP-Address}}' \\
    WHERE acctuniqueid = '%{{Acct-Unique-Session-Id}}'"

accounting_stop_query = "\\
    UPDATE {schema_name}.radacct \\
    SET acctstoptime = NOW(), \\
        acctsessiontime = %{{%{{Acct-Session-Time}}:-0}}, \\
        acctinputoctets = %{{%{{Acct-Input-Gigawords}}:-0}} * 4294967296 + %{{%{{Acct-Input-Octets}}:-0}}, \\
        acctoutputoctets = %{{%{{Acct-Output-Gigawords}}:-0}} * 4294967296 + %{{%{{Acct-Output-Octets}}:-0}}, \\
        acctterminatecause = '%{{Acct-Terminate-Cause}}', \\
        framedipaddress = '%{{Framed-IP-Address}}' \\
    WHERE acctuniqueid = '%{{Acct-Unique-Session-Id}}'"

#######################################################################
# Post-Auth Logging
#######################################################################

post-auth {{
    query = "\\
        INSERT INTO {schema_name}.radpostauth \\
            (username, password, reply, authdate, nasipaddress, callingstationid) \\
        VALUES \\
            ('%{{SQL-User-Name}}', \\
             '%{{%{{User-Password}}:-%{{Chap-Password}}}}', \\
             '%{{reply:Packet-Type}}', \\
             NOW(), \\
             '%{{NAS-IP-Address}}', \\
             '%{{Calling-Station-Id}}')"
}}
'''
        
        output_path.write_text(queries_content)
        logger.info(f"Generated queries.conf for tenant: {schema_name}")
    
    def _generate_tenant_env_file(
        self,
        schema_name: str,
        tenant_name: str,
        output_path: Path
    ):
        """Generate Docker environment file for tenant's RADIUS container."""
        
        # Get database settings from Django
        db_settings = settings.DATABASES.get('default', {})
        
        env_content = f'''# FreeRADIUS Configuration for Tenant: {tenant_name}
# Schema: {schema_name}
# Auto-generated by Netily ISP

# Database Connection
DB_HOST=host.docker.internal
DB_PORT={db_settings.get('PORT', 5432)}
DB_NAME={db_settings.get('NAME', 'isp_management')}
DB_USER={db_settings.get('USER', 'postgres')}
DB_PASS={db_settings.get('PASSWORD', 'postgres')}
DB_SCHEMA={schema_name}

# RADIUS Settings
RADIUS_SECRET=netily_{schema_name}_secret
RADIUS_DEBUG=no

# Tenant Info
TENANT_NAME={tenant_name}
TENANT_SCHEMA={schema_name}
'''
        
        output_path.write_text(env_content)
        logger.info(f"Generated .env file for tenant: {schema_name}")
    
    def _register_tenant(self, schema_name: str, tenant_name: str):
        """Register tenant in the radius tenant registry."""
        from ..models import RadiusTenantConfig
        
        RadiusTenantConfig.objects.update_or_create(
            schema_name=schema_name,
            defaults={
                'tenant_name': tenant_name,
                'is_active': True,
            }
        )
    
    # ────────────────────────────────────────────────────────────────
    # DYNAMIC QUERIES.CONF MANAGEMENT
    # ────────────────────────────────────────────────────────────────
    
    def get_active_schema_queries_path(self) -> str:
        """Get the path to the active queries.conf for current tenant."""
        schema = self.get_current_tenant_schema()
        
        if schema == 'public':
            return str(self.radius_config_path / 'mods-config' / 'sql' / 'main' / 'postgresql' / 'queries.conf')
        
        tenant_path = self.radius_config_path / 'tenants' / schema / 'queries.conf'
        if tenant_path.exists():
            return str(tenant_path)
        
        # Auto-configure if doesn't exist
        self.configure_tenant_radius(schema)
        return str(tenant_path)
    
    def update_main_queries_conf(self, schema_name: str):
        """
        Update the main queries.conf to point to a specific tenant.
        
        This is used when running a single shared RADIUS instance.
        After updating, the RADIUS container should be restarted.
        """
        main_queries_path = self.radius_config_path / 'mods-config' / 'sql' / 'main' / 'postgresql' / 'queries.conf'
        tenant_queries_path = self.radius_config_path / 'tenants' / schema_name / 'queries.conf'
        
        if not tenant_queries_path.exists():
            self.configure_tenant_radius(schema_name)
        
        # Copy tenant config to main location
        import shutil
        shutil.copy(tenant_queries_path, main_queries_path)
        
        logger.info(f"Updated main queries.conf for tenant: {schema_name}")
        return str(main_queries_path)
    
    # ────────────────────────────────────────────────────────────────
    # NAS → TENANT MAPPING
    # ────────────────────────────────────────────────────────────────
    
    def get_nas_tenant_map(self) -> Dict[str, str]:
        """
        Get mapping of NAS IP addresses to tenant schemas.
        
        This is used for dynamic tenant routing in shared RADIUS mode.
        """
        cached = cache.get(self.NAS_TENANT_MAP_CACHE_KEY)
        if cached:
            return cached
        
        from ..models import Nas
        
        mapping = {}
        for nas in Nas.objects.select_related('router').all():
            if nas.router and hasattr(nas.router, 'tenant_subdomain'):
                schema = f"tenant_{nas.router.tenant_subdomain}" if nas.router.tenant_subdomain else None
                if schema:
                    mapping[str(nas.nasname)] = schema
        
        cache.set(self.NAS_TENANT_MAP_CACHE_KEY, mapping, timeout=300)
        return mapping
    
    def get_tenant_for_nas(self, nas_ip: str) -> Optional[str]:
        """Get tenant schema for a specific NAS IP."""
        mapping = self.get_nas_tenant_map()
        return mapping.get(nas_ip)
    
    # ────────────────────────────────────────────────────────────────
    # DOCKER MANAGEMENT
    # ────────────────────────────────────────────────────────────────
    
    def generate_docker_compose_override(self) -> str:
        """
        Generate docker-compose.override.yml with tenant RADIUS services.
        
        This creates a separate RADIUS container for each tenant.
        """
        from ..models import RadiusTenantConfig
        
        services = {}
        for tenant in RadiusTenantConfig.objects.filter(is_active=True):
            service_name = f"radius_{tenant.schema_name.replace('tenant_', '')}"
            services[service_name] = {
                'build': {
                    'context': '..',
                    'dockerfile': 'docker/Dockerfile.radius',
                },
                'container_name': f"netily_radius_{tenant.schema_name.replace('tenant_', '')}",
                'env_file': f".env.radius.{tenant.schema_name}",
                'ports': [
                    # Each tenant gets unique ports
                    f"{1812 + tenant.id}:{1812}/udp",
                    f"{1813 + tenant.id}:{1813}/udp",
                ],
                'extra_hosts': ['host.docker.internal:host-gateway'],
                'restart': 'unless-stopped',
            }
        
        override_content = {
            'version': '3.8',
            'services': services,
        }
        
        import yaml
        output_path = self.docker_path / 'docker-compose.override.yml'
        with open(output_path, 'w') as f:
            yaml.dump(override_content, f, default_flow_style=False)
        
        logger.info(f"Generated docker-compose.override.yml with {len(services)} tenant RADIUS services")
        return str(output_path)
    
    def restart_radius_container(self, schema_name: str = None) -> bool:
        """
        Restart RADIUS container(s) to apply configuration changes.
        
        Args:
            schema_name: Specific tenant to restart, or None for main container
        """
        try:
            container_name = f"netily_radius_{schema_name.replace('tenant_', '')}" if schema_name else "netily_radius"
            
            subprocess.run(
                ['docker', 'restart', container_name],
                capture_output=True,
                check=True,
                cwd=str(self.docker_path)
            )
            
            logger.info(f"Restarted RADIUS container: {container_name}")
            return True
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to restart RADIUS container: {e}")
            return False
    
    def rebuild_radius_container(self) -> bool:
        """Rebuild and restart the main RADIUS container."""
        try:
            subprocess.run(
                ['docker-compose', 'build', '--no-cache', 'radius'],
                capture_output=True,
                check=True,
                cwd=str(self.docker_path)
            )
            
            subprocess.run(
                ['docker-compose', 'up', '-d', 'radius'],
                capture_output=True,
                check=True,
                cwd=str(self.docker_path)
            )
            
            logger.info("Rebuilt and restarted RADIUS container")
            return True
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to rebuild RADIUS container: {e}")
            return False


# Singleton instance
tenant_radius_service = TenantRadiusConfigService()
