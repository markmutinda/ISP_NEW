from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from apps.network.models.router_models import Router
from apps.radius.models import Nas
from apps.vpn.services.vpn_provisioning_service import VPNProvisioningService
from django.db import connection
import logging

logger = logging.getLogger(__name__)

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


@receiver(post_save, sender=Router)
def handle_router_lifecycle(sender, instance, created, **kwargs):
    """
    Orchestrates the Router lifecycle:
    1. Provision VPN (IP, Certs, CCD) if needed.
    2. Sync to RADIUS NAS whitelist ONLY if an IP exists.
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
            else:
                logger.debug(f"[GLOBAL CLEANUP] No GlobalRouterMap entry found for {instance.name} ({nas_ip})")
        except Exception as e:
            logger.error(f"[GLOBAL CLEANUP] Failed to remove GlobalRouterMap entry for {instance.name} ({nas_ip}): {e}")
    else:
        logger.warning(f"[GLOBAL CLEANUP] {instance.name} has no IP address to clean up from GlobalRouterMap")