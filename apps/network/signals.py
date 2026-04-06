from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.db import transaction
from django.core.cache import cache
from apps.network.models.router_models import Router
from apps.radius.models import Nas
from apps.vpn.services.vpn_provisioning_service import VPNProvisioningService
from django.db import connection
from django_tenants.utils import schema_context
import logging
import subprocess

logger = logging.getLogger(__name__)

RADIUS_RELOAD_LOCK_KEY = "radius_reload_lock"
RADIUS_RELOAD_LOCK_TTL = 8  # seconds

def sanitize_string(value: str) -> str:
    """
    Remove surrogate characters from a string by encoding to UTF-8 with replacement.
    This prevents 'surrogates not allowed' errors when the string is later encoded
    (e.g., during database storage or encryption).
    """
    if value is None:
        return ""
    # Encode to bytes with replacement for any invalid surrogates, then decode back
    return value.encode('utf-8', 'replace').decode('utf-8')


def reload_radius_clients_now() -> None:
    """
    Force FreeRADIUS to pick up new SQL clients by container restart.
    Debounced to avoid restart storms on repeated router saves.
    """
    # add() returns False if key already exists
    if not cache.add(RADIUS_RELOAD_LOCK_KEY, "1", timeout=RADIUS_RELOAD_LOCK_TTL):
        logger.info("[RADIUS RELOAD] Skipped (debounced).")
        return

    cmd = ["docker", "restart", "netily_radius"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            logger.info("[RADIUS RELOAD] Restarted netily_radius successfully.")
        else:
            logger.error(
                "[RADIUS RELOAD] Restart failed rc=%s stderr=%s",
                r.returncode, (r.stderr or "").strip()
            )
    except Exception as e:
        logger.exception("[RADIUS RELOAD] Restart exception: %s", e)


@receiver(post_save, sender=Router)
def handle_router_lifecycle(sender, instance, created, **kwargs):
    """
    Orchestrates the Router lifecycle:
    1. Provision VPN (IP, Certs, CCD) if needed.
    2. Sync to RADIUS NAS whitelist ONLY if an IP exists.
    3. Trigger RADIUS client reload after commit.
    """
    # 1. Prevent infinite loops during provisioning saves
    if kwargs.get('update_fields') and 'vpn_provisioned' in kwargs.get('update_fields'):
        return

    # 2. AUTO-PROVISION VPN
    # If the router is new and OpenVPN is enabled, provision it immediately
    if (created or not instance.vpn_provisioned) and instance.enable_openvpn:
        try:
            logger.info(f"[VPN AUTO-PROVISION] Creating tunnel for {instance.name}...")
            service = VPNProvisioningService()
            # This service will assign vpn_ip_address and call .save() internally
            service.provision_router(instance)
            # The .save() inside provision_router will trigger this signal again 
            # with the new IP, so we can exit this execution safely.
            return 
        except Exception as e:
            logger.error(f"VPN Auto-provisioning failed for {instance.name}: {e}")
            return

    # 3. AUTO-SYNC TO RADIUS NAS
    # CRITICAL: Only sync if we have a valid, unique VPN IP to avoid UniqueViolations
    if not instance.vpn_ip_address:
        logger.info(f"[RADIUS SYNC] Skipping {instance.name} - No VPN IP assigned yet.")
        return

    nas_ip = instance.vpn_ip_address

    # Build the secret and sanitize it to remove any surrogate characters
    raw_secret = instance.shared_secret or f"netily_{connection.schema_name}_secret"
    secret = sanitize_string(raw_secret)

    # Clean up old entry to prevent duplicates if the name/IP changed
    Nas.objects.filter(shortname=instance.name).delete()

    # Create the fresh NAS entry in this tenant's schema
    try:
        Nas.objects.create(
            nasname=nas_ip,
            shortname=instance.name,
            type='mikrotik',
            secret=secret,
            server='Default'
        )
        logger.info(f"[RADIUS AUTO-SYNC] Added {instance.name} ({nas_ip}) to {connection.schema_name} NAS table.")
        
        # Trigger RADIUS client reload after DB commit succeeds
        transaction.on_commit(reload_radius_clients_now)
        
    except Exception as e:
        logger.error(f"RADIUS NAS sync failed for {instance.name}: {e}")


@receiver(post_delete, sender=Router)
def cleanup_router_radius_nas(sender, instance, **kwargs):
    """
    Ensures that when a Router is deleted, its entry is wiped from 
    the RADIUS NAS table, even if the foreign key link was broken.
    """
    try:
        # We delete by shortname to ensure orphaned entries are caught
        deleted_count, _ = Nas.objects.filter(shortname=instance.name).delete()
        if deleted_count > 0:
            logger.info(f"[RADIUS CLEANUP] Removed {instance.name} from {connection.schema_name} NAS table.")
            
            # Trigger RADIUS client reload after DB commit succeeds
            transaction.on_commit(reload_radius_clients_now)
    except Exception as e:
        logger.error(f"Failed to cleanup RADIUS NAS for {instance.name}: {e}")


# ────────────────────────────────────────────────────────────────
# GLOBAL ROUTER MAP CLEANUP (PUBLIC SCHEMA)
# ────────────────────────────────────────────────────────────────

@receiver(post_delete, sender=Router)
def cleanup_global_router_map(sender, instance, **kwargs):
    """
    Ensures that when a router is deleted from a tenant, 
    its entry in the public RADIUS client table is also removed.
    
    This is critical for:
    1. Preventing "ghost" router entries in the global RADIUS client table
    2. Avoiding IP conflicts when re-using VPN IP addresses
    3. Keeping the shared RADIUS server's client list clean
    """
    # Import here to avoid circular imports
    try:
        from apps.core.models import GlobalRouterMap
    except ImportError:
        logger.warning("GlobalRouterMap not found - skipping cleanup")
        return

    # Use the VPN IP to find and kill the entry in the shared RADIUS table
    # The VPN IP is the primary identifier for the router in the global map
    nas_ip = instance.vpn_ip_address
    
    # Fallback to regular IP address if VPN IP not set (shouldn't happen in prod)
    if not nas_ip:
        nas_ip = getattr(instance, 'ip_address', None)
        if nas_ip:
            logger.warning(f"[GLOBAL CLEANUP] {instance.name} had no VPN IP, using IP {nas_ip} for cleanup")
    
    if nas_ip:
        try:
            deleted_count = GlobalRouterMap.objects.filter(nas_ip=nas_ip).delete()[0]
            if deleted_count > 0:
                logger.info(f"[GLOBAL CLEANUP] Removed {instance.name} ({nas_ip}) from GlobalRouterMap.")
                
                # Trigger RADIUS client reload after DB commit succeeds
                transaction.on_commit(reload_radius_clients_now)
            else:
                logger.debug(f"[GLOBAL CLEANUP] No GlobalRouterMap entry found for {instance.name} ({nas_ip})")
        except Exception as e:
            logger.error(f"[GLOBAL CLEANUP] Failed to remove GlobalRouterMap entry for {instance.name} ({nas_ip}): {e}")
    else:
        logger.warning(f"[GLOBAL CLEANUP] {instance.name} has no IP address to clean up from GlobalRouterMap")


# ────────────────────────────────────────────────────────────────
# ROUTER TENANT INDEX SYNC (PUBLIC SCHEMA)
# ────────────────────────────────────────────────────────────────

def _get_current_tenant_for_index():
    """
    Resolve current tenant object from active schema.
    """
    from apps.core.models import Tenant
    schema_name = getattr(connection, 'schema_name', None)
    if not schema_name or schema_name == 'public':
        return None
    with schema_context('public'):
        return Tenant.objects.filter(schema_name=schema_name).first()


@receiver(post_save, sender=Router)
def upsert_router_tenant_index(sender, instance, **kwargs):
    """
    Keep RouterTenantIndex in sync for O(1) auth_key -> tenant lookup.
    """
    try:
        from apps.core.models import RouterTenantIndex
    except ImportError:
        logger.warning("RouterTenantIndex not found - skipping index upsert")
        return

    tenant = _get_current_tenant_for_index()
    if not tenant:
        return

    try:
        with schema_context('public'):
            RouterTenantIndex.objects.update_or_create(
                router_auth_key=instance.auth_key,
                defaults={
                    'router_id': instance.id,  # Store the router UUID for ID-based lookups
                    'tenant': tenant,
                    'tenant_schema': tenant.schema_name,
                    'router_name': instance.name or '',
                    'is_active': instance.is_active,
                }
            )
            logger.debug(f"[INDEX UPSERT] Updated index for router {instance.name} (ID: {instance.id})")
    except Exception as e:
        logger.error(f"[INDEX UPSERT] Failed for {instance.name}: {e}")


@receiver(post_delete, sender=Router)
def cleanup_router_tenant_index(sender, instance, **kwargs):
    """
    Clean up RouterTenantIndex entry when a router is deleted.
    """
    try:
        from apps.core.models import RouterTenantIndex
    except ImportError:
        logger.warning("RouterTenantIndex not found - skipping index cleanup")
        return

    try:
        with schema_context('public'):
            # Clean up by both auth_key and router_id to ensure complete cleanup
            deleted_count = RouterTenantIndex.objects.filter(
                router_auth_key=instance.auth_key
            ).delete()[0]
            if deleted_count > 0:
                logger.debug(f"[INDEX CLEANUP] Removed index entry for router {instance.name} (ID: {instance.id})")
    except Exception as e:
        logger.error(f"[INDEX CLEANUP] Failed for {instance.name}: {e}")