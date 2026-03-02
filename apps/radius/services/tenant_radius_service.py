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
    SELECT radacctid, acctsessionid, username, nasipaddress, \\
           nasidentifier, nasportid, nasporttype, acctstarttime, acctupdatetime, \\
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
         nasidentifier, nasportid, nasporttype, acctstarttime, acctupdatetime, \\
         acctstoptime, acctsessiontime, acctauthentic, connectinfo_start, \\
         acctinputoctets, acctoutputoctets, calledstationid, \\
         callingstationid, acctterminatecause, servicetype, framedprotocol, \\
         framedipaddress) \\
    VALUES \\
        ('%{{Acct-Session-Id}}', '%{{Acct-Unique-Session-Id}}', \\
         '%{{SQL-User-Name}}', '%{{Realm}}', '%{{NAS-IP-Address}}', \\
         '%{{NAS-Identifier}}', '%{{NAS-Port-Id}}', '%{{NAS-Port-Type}}', NOW(), NOW(), \\
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
        framedipaddress = '%{{Framed-IP-Address}}', \\
        nasidentifier = '%{{NAS-Identifier}}' \\
    WHERE acctuniqueid = '%{{Acct-Unique-Session-Id}}'"

accounting_stop_query = "\\
    UPDATE {schema_name}.radacct \\
    SET acctstoptime = NOW(), \\
        acctsessiontime = %{{%{{Acct-Session-Time}}:-0}}, \\
        acctinputoctets = %{{%{{Acct-Input-Gigawords}}:-0}} * 4294967296 + %{{%{{Acct-Input-Octets}}:-0}}, \\
        acctoutputoctets = %{{%{{Acct-Output-Gigawords}}:-0}} * 4294967296 + %{{%{{Acct-Output-Octets}}:-0}}, \\
        acctterminatecause = '%{{Acct-Terminate-Cause}}', \\
        framedipaddress = '%{{Framed-IP-Address}}', \\
        nasidentifier = '%{{NAS-Identifier}}' \\
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
        
        # Get tenant config to include ports
        from ..models import RadiusTenantConfig
        try:
            tenant_config = RadiusTenantConfig.objects.get(schema_name=schema_name)
            auth_port = tenant_config.auth_port
            acct_port = tenant_config.acct_port
        except RadiusTenantConfig.DoesNotExist:
            auth_port = '1814'  # Default fallback
            acct_port = '1815'  # Default fallback
        
        env_content = f'''# FreeRADIUS Configuration for Tenant: {tenant_name}
# Schema: {schema_name}

# Database Connection
DB_HOST=netily_db
DB_PORT=5432
DB_NAME={db_settings.get('NAME', 'isp_management')}
DB_USER={db_settings.get('USER', 'postgres')}
DB_PASS={db_settings.get('PASSWORD', 'postgres')}
DB_SCHEMA={schema_name}

# RADIUS Settings
RADIUS_SECRET=netily_{schema_name}_secret

# --- THE FIX: DIRECT QUERY OVERRIDES ---
# We force FreeRADIUS to use our new schema-qualified query with nasidentifier included
# This bypasses the need for the queries.conf file entirely!

POSTGRESQL_ACCOUNTING_START_QUERY="INSERT INTO {schema_name}.radacct (acctsessionid, acctuniqueid, username, nasipaddress, nasidentifier, nasportid, nasporttype, acctstarttime, acctupdatetime, acctstoptime, acctsessiontime, acctauthentic, connectinfo_start, acctinputoctets, acctoutputoctets, calledstationid, callingstationid, acctterminatecause, servicetype, framedprotocol, framedipaddress) VALUES ('%{{Acct-Session-Id}}', '%{{Acct-Unique-Session-Id}}', '%{{SQL-User-Name}}', '%{{NAS-IP-Address}}', '%{{NAS-Identifier}}', '%{{NAS-Port-Id}}', '%{{NAS-Port-Type}}', NOW(), NOW(), NULL, 0, '%{{Acct-Authentic}}', '%{{Connect-Info}}', 0, 0, '%{{Called-Station-Id}}', '%{{Calling-Station-Id}}', '', '%{{Service-Type}}', '%{{Framed-Protocol}}', '%{{Framed-IP-Address}}')"

POSTGRESQL_ACCOUNTING_INTERIM_QUERY="UPDATE {schema_name}.radacct SET acctupdatetime = NOW(), acctinterval = %{{%{{Acct-Session-Time}}:-0}} - acctsessiontime, acctsessiontime = %{{%{{Acct-Session-Time}}:-0}}, acctinputoctets = %{{%{{Acct-Input-Gigawords}}:-0}} * 4294967296 + %{{%{{Acct-Input-Octets}}:-0}}, acctoutputoctets = %{{%{{Acct-Output-Gigawords}}:-0}} * 4294967296 + %{{%{{Acct-Output-Octets}}:-0}}, framedipaddress = '%{{Framed-IP-Address}}', nasidentifier = '%{{NAS-Identifier}}' WHERE acctuniqueid = '%{{Acct-Unique-Session-Id}}'"

POSTGRESQL_ACCOUNTING_STOP_QUERY="UPDATE {schema_name}.radacct SET acctstoptime = NOW(), acctsessiontime = %{{%{{Acct-Session-Time}}:-0}}, acctinputoctets = %{{%{{Acct-Input-Gigawords}}:-0}} * 4294967296 + %{{%{{Acct-Input-Octets}}:-0}}, acctoutputoctets = %{{%{{Acct-Output-Gigawords}}:-0}} * 4294967296 + %{{%{{Acct-Output-Octets}}:-0}}, acctterminatecause = '%{{Acct-Terminate-Cause}}', framedipaddress = '%{{Framed-IP-Address}}', nasidentifier = '%{{NAS-Identifier}}' WHERE acctuniqueid = '%{{Acct-Unique-Session-Id}}'"
# ---------------------------------------

AUTH_PORT={auth_port}
ACCT_PORT={acct_port}
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
        The networks are defined in the main docker-compose.yml, so we don't
        need to redefine them here.
        """
        from ..models import RadiusTenantConfig
        
        services = {}
        for tenant in RadiusTenantConfig.objects.filter(is_active=True):
            # Clean the schema name for service naming
            tenant_slug = tenant.schema_name.replace('tenant_', '')
            service_name = f"radius_{tenant_slug}"
            
            # --- INTELLIGENT CHANGE: USE DB PORTS FROM MODEL ---
            # Ensure ports are assigned
            if not tenant.auth_port or not tenant.acct_port:
                tenant.assign_ports()
                tenant.save()
            
            services[service_name] = {
                'build': {
                    'context': '..',
                    'dockerfile': 'docker/Dockerfile.radius',
                },
                'container_name': f"netily_radius_{tenant_slug}",
                'env_file': f".env.radius.{tenant.schema_name}",
                'ports': [
                    f"{tenant.auth_port}:1812/udp",  # Uses DB value (e.g., 1814)
                    f"{tenant.acct_port}:1813/udp",  # Uses DB value (e.g., 1815)
                ],
                'networks': [
                    'isp_network',
                    'vpn_network'
                ],
                'extra_hosts': [
                    'host.docker.internal:host-gateway'
                ],
                # ⬇️ VOLUMES BLOCK REMOVED - Using docker cp injection instead ⬇️
                'restart': 'unless-stopped',
            }
        
        # Only output the services - networks are inherited from main compose file
        override_content = {
            'services': services
        }
        
        import yaml
        output_path = self.docker_path / 'docker-compose.override.yml'
        with open(output_path, 'w') as f:
            yaml.dump(override_content, f, default_flow_style=False)
        
        logger.info(f"Generated docker-compose.override.yml with {len(services)} tenant RADIUS services")
        return str(output_path)
    
    # ────────────────────────────────────────────────────────────────
    # TARGETED DOCKER MANAGEMENT (Zero-Downtime)
    # ────────────────────────────────────────────────────────────────

    def deploy_tenant_radius(self, schema_name: str) -> bool:
        """
        Deploys ONLY the specific tenant's RADIUS container, then injects 
        the customized queries.conf file directly to avoid Docker volume bugs.
        """
        tenant_slug = schema_name.replace('tenant_', '')
        service_name = f"radius_{tenant_slug}"
        container_name = f"netily_radius_{tenant_slug}"

        # 1. Regenerate the override file first so compose knows about the new service
        self.generate_docker_compose_override()

        try:
            print(f"🚀 [ISP PROVISIONING] Starting targeted deploy for {service_name}...")
            # We target ONLY the specific service name (e.g., radius_monicah)
            subprocess.run(
                ['docker-compose', 'up', '-d', '--build', service_name],
                capture_output=True,
                check=True,
                cwd=str(self.docker_path)
            )
            
            # 2. INJECT THE SQL FILE DIRECTLY INTO THE CONTAINER
            print(f"💉 Injecting SQL queries into {container_name}...")
            queries_source = str(self.radius_config_path / 'tenants' / schema_name / 'queries.conf')
            
            subprocess.run(
                ['docker', 'cp', queries_source, f"{container_name}:/etc/freeradius/mods-config/sql/main/postgresql/queries.conf"],
                capture_output=True, check=True
            )
            
            # 3. Restart container so it loads the injected file
            subprocess.run(['docker', 'restart', container_name], capture_output=True, check=True)
            
            logger.info(f"Successfully deployed and injected isolated RADIUS for {schema_name}")
            
            # 4. Generate/update the port mapping file for OpenVPN routing
            self.generate_port_mapping_file()
            
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to deploy isolated RADIUS: {e.stderr.decode()}")
            return False

    def remove_tenant_radius(self, schema_name: str) -> bool:
        """
        Stops and removes ONLY the specific tenant's container using the Docker CLI.
        This avoids triggering a stack-wide 're-sync' from Docker Compose.
        """
        tenant_slug = schema_name.replace('tenant_', '')
        container_name = f"netily_radius_{tenant_slug}"

        print(f"🗑️ [ISP CLEANUP] Destroying isolated container {container_name}...")

        try:
            # 1. Use raw Docker CLI to stop and remove (does not affect other services)
            subprocess.run(['docker', 'stop', container_name], capture_output=True)
            subprocess.run(['docker', 'rm', container_name], capture_output=True)

            # 2. Update the override file so it stays clean for future global restarts
            self.generate_docker_compose_override()

            # 3. Delete the tenant-specific .env file
            env_path = self.docker_path / f'.env.radius.{schema_name}'
            if env_path.exists():
                os.remove(env_path)

            # --- THE NEW FIX: DELETE THE CONFIG FOLDER ---
            import shutil
            tenant_config_dir = self.radius_config_path / 'tenants' / schema_name
            if tenant_config_dir.exists():
                shutil.rmtree(tenant_config_dir)
                print(f"🗑️ Physically deleted config folder: {tenant_config_dir}")
            # ---------------------------------------------

            # 4. Generate/update the port mapping file for OpenVPN routing
            self.generate_port_mapping_file()

            print(f"✅ [ISP CLEANUP] Isolated teardown complete for {schema_name}.")
            return True
        except Exception as e:
            logger.error(f"Failed to teardown isolated RADIUS: {e}")
            return False
    
    # ────────────────────────────────────────────────────────────────
    # PORT MAPPING FOR OPENVPN ROUTING
    # ────────────────────────────────────────────────────────────────

    def generate_port_mapping_file(self):
        """
        Generates the port map and a Zero-Downtime sync script for OpenVPN.
        """
        from ..models import RadiusTenantConfig
        
        ports_dir = self.radius_config_path / 'ports'
        ports_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Generate the Map File
        map_file = ports_dir / 'tenant_ports.conf'
        lines = ["# Auto-generated by Netily ISP", "base 1812 1813"]  # Always include base
        for config in RadiusTenantConfig.objects.filter(is_active=True):
            if config.auth_port and config.acct_port:
                tenant_slug = config.schema_name.replace('tenant_', '')
                lines.append(f"{tenant_slug} {config.auth_port} {config.acct_port}")
        
        map_file.write_text('\n'.join(lines) + '\n')
        logger.info(f"Generated port mapping file with {len(lines) - 2} tenants")

    def restart_radius_container(self, schema_name: str = None) -> bool:
        """
        Restart RADIUS container(s) to apply configuration changes.
        
        Args:
            schema_name: Specific tenant to restart, or None for main container
        """
        try:
            if schema_name:
                container_name = f"netily_radius_{schema_name.replace('tenant_', '')}"
            else:
                container_name = "netily_radius"
            
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