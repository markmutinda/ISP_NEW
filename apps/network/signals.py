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