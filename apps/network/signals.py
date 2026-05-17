from django.db.models.signals import post_save, post_delete, pre_delete
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
    3. Assign HAProxy remote ports after VPN provisioning.
    4. Trigger RADIUS client reload after commit.
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
        
        # 4. HAPROXY REMOTE PORT ASSIGNMENT
        # After VPN is provisioned, assign remote ports for Winbox/API access
        if instance.vpn_ip_address and not instance.winbox_remote_port:
            try:
                from apps.network.services.haproxy_manager import (
                    sync_haproxy_config, 
                    get_router_winbox_port, 
                    get_router_api_port
                )
                
                # Get unique ports for this router
                instance.winbox_remote_port = get_router_winbox_port(instance.id)
                instance.api_remote_port = get_router_api_port(instance.id)
                
                # Update the router with assigned ports
                Router.objects.filter(pk=instance.pk).update(
                    winbox_remote_port=instance.winbox_remote_port,
                    api_remote_port=instance.api_remote_port,
                )
                
                logger.info(
                    f"[HAPROXY] Assigned ports to {instance.name}: "
                    f"Winbox={instance.winbox_remote_port}, API={instance.api_remote_port}"
                )
                
                # Sync HAProxy configuration in background after commit succeeds
                transaction.on_commit(lambda: sync_haproxy_config())
                
            except Exception as e:
                logger.error(f"[HAPROXY] Port assignment failed for {instance.name}: {e}")
        
        # 5. Trigger RADIUS client reload after DB commit succeeds
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
# PRE-DELETE VPN CLEANUP (RUNS BEFORE ROW IS DELETED)
# ────────────────────────────────────────────────────────────────

@receiver(pre_delete, sender=Router)
def deprovision_vpn_before_delete(sender, instance, **kwargs):
    """
    Free all VPN resources BEFORE the router row is deleted so that:
      - The VPN IP is returned to the pool immediately.
      - The OpenVPN CCD file is removed (router can no longer connect).
      - The certificate is revoked.
      - The WireGuard peer is removed from the container.

    We use pre_delete (not post_delete) because post_delete signals
    receive an instance with no PK, making save() impossible, and
    the VPN provisioning service needs the instance intact.
    """
    if not instance.vpn_provisioned and not instance.vpn_ip_address:
        return

    logger.info(
        f"[PRE-DELETE] Cleaning up VPN for router '{instance.name}' "
        f"(ID: {instance.id}, IP: {instance.vpn_ip_address})"
    )

    # 1. Revoke certificate
    try:
        if instance.vpn_certificate_id:
            # Check if certificate still exists and hasn't been revoked already
            cert = instance.vpn_certificate
            if cert and not cert.is_revoked:
                cert.revoke(
                    reason=f"Router '{instance.name}' deleted"
                )
                logger.info(f"[PRE-DELETE] Certificate revoked for {instance.name}")
            elif cert and cert.is_revoked:
                logger.info(f"[PRE-DELETE] Certificate already revoked for {instance.name}")
    except Exception as e:
        logger.warning(f"[PRE-DELETE] Certificate revocation failed: {e}")

    # 2. Remove CCD file (filename = openvpn_username)
    try:
        if instance.openvpn_username:
            from apps.vpn.services.ccd_manager import CCDManager
            ccd = CCDManager()
            ccd.remove_ccd_file(instance.openvpn_username)
            logger.info(f"[PRE-DELETE] CCD file removed for {instance.openvpn_username}")
    except Exception as e:
        logger.warning(f"[PRE-DELETE] CCD removal failed: {e}")

    # 3. Free the VPN IP from the global map
    try:
        if instance.vpn_ip_address:
            from apps.core.models import GlobalRouterMap
            with schema_context('public'):
                deleted_count, _ = GlobalRouterMap.objects.filter(
                    nas_ip=instance.vpn_ip_address
                ).delete()
                if deleted_count:
                    logger.info(
                        f"[PRE-DELETE] Freed VPN IP {instance.vpn_ip_address} "
                        f"from GlobalRouterMap"
                    )
                else:
                    logger.debug(
                        f"[PRE-DELETE] No GlobalRouterMap entry found for IP {instance.vpn_ip_address}"
                    )
    except Exception as e:
        logger.warning(f"[PRE-DELETE] GlobalRouterMap cleanup failed: {e}")

    # 4. Optionally: Remove from the IP pool if you maintain one
    try:
        from apps.vpn.services.ip_pool_manager import IPPoolManager
        if instance.vpn_ip_address:
            pool_manager = IPPoolManager()
            pool_manager.release_ip(instance.vpn_ip_address)
            logger.info(f"[PRE-DELETE] Released VPN IP {instance.vpn_ip_address} back to pool")
    except ImportError:
        logger.debug("[PRE-DELETE] IPPoolManager not available — skipping pool release")
    except Exception as e:
        logger.warning(f"[PRE-DELETE] IP pool release failed: {e}")

    # 5. Remove the WireGuard peer from the container (FIX FOR CLUTTER BUG)
    # Without this, wg show output accumulates stale public keys forever.
    try:
        if instance.wireguard_public_key:
            from apps.vpn.services.wireguard_manager import remove_peer
            remove_peer(instance.wireguard_public_key)
            logger.info(f"[PRE-DELETE] WireGuard peer removed for {instance.name} (key: {instance.wireguard_public_key[:16]}...)")
        else:
            logger.debug(f"[PRE-DELETE] No WireGuard public key for {instance.name} — skipping peer removal")
    except ImportError:
        logger.debug("[PRE-DELETE] wireguard_manager not available — skipping peer removal")
    except Exception as e:
        logger.warning(f"[PRE-DELETE] WireGuard peer removal failed for {instance.name}: {e}")


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
    
    NOTE: This runs AFTER pre_delete. If pre_delete already removed the entry,
    this will just log a debug message.
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
            with schema_context('public'):
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

    # Also clean up HAProxy remote port assignment if needed
    try:
        from apps.network.services.haproxy_manager import sync_haproxy_config
        # Trigger HAProxy config sync to remove this router's entries
        transaction.on_commit(lambda: sync_haproxy_config())
        logger.info(f"[HAPROXY CLEANUP] Triggered config sync for deleted router {instance.name}")
    except Exception as e:
        logger.warning(f"[HAPROXY CLEANUP] Failed to trigger config sync: {e}")


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
    Keep RouterTenantIndex in sync for public O(1) auth_key -> tenant lookup.

    IMPORTANT:
    - router_auth_key is globally unique and safe for public provisioning lookup.
    - router_id is tenant-local and can repeat across schemas.
    - RouterTenantIndex must be written in public schema.
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
                    'router_id': instance.id,
                    'tenant': tenant,
                    'tenant_schema': tenant.schema_name,
                    'router_name': instance.name or '',
                    'is_active': instance.is_active,
                },
            )

        logger.debug(
            "[INDEX UPSERT] Updated router index: name=%s schema=%s id=%s auth_key=%s",
            instance.name,
            tenant.schema_name,
            instance.id,
            instance.auth_key,
        )

    except Exception as e:
        logger.error(
            "[INDEX UPSERT] Failed for router=%s schema=%s id=%s: %s",
            instance.name,
            getattr(tenant, 'schema_name', None),
            instance.id,
            e,
        )


@receiver(post_delete, sender=Router)
def cleanup_router_tenant_index(sender, instance, **kwargs):
    """
    Clean up RouterTenantIndex entry when a router is deleted.

    Cleanup by auth_key is safe because auth_key is globally unique.
    """
    try:
        from apps.core.models import RouterTenantIndex
    except ImportError:
        logger.warning("RouterTenantIndex not found - skipping index cleanup")
        return

    try:
        with schema_context('public'):
            deleted_count = RouterTenantIndex.objects.filter(
                router_auth_key=instance.auth_key
            ).delete()[0]

        if deleted_count > 0:
            logger.debug(
                "[INDEX CLEANUP] Removed index entry for router %s "
                "(schema=%s, id=%s)",
                instance.name,
                instance._state.db if hasattr(instance, '_state') else 'unknown',
                instance.id,
            )

    except Exception as e:
        logger.error(
            "[INDEX CLEANUP] Failed for router=%s auth_key=%s: %s",
            instance.name,
            instance.auth_key,
            e,
        )


# ────────────────────────────────────────────────────────────────
# AUTO-CLONE GLOBAL HOTSPOT PLANS TO NEW ROUTERS
# ────────────────────────────────────────────────────────────────

@receiver(post_save, sender=Router)
def clone_global_hotspot_plans_to_new_router(sender, instance, created, **kwargs):
    """
    When a new Router is created, find all HotspotPlans marked as
    is_global_template=True and clone one copy of each unique plan
    name to this new router.
    """
    if not created:
        return

    try:
        from apps.billing.models.hotspot_models import HotspotPlan

        seen_names = set()
        plans_cloned = 0

        # Grab one representative plan per unique name from global templates
        for plan in HotspotPlan.objects.filter(
            is_global_template=True
        ).order_by('name', 'sort_order', 'id'):

            if plan.name in seen_names:
                continue  # Skip duplicates — same plan exists on multiple routers
            seen_names.add(plan.name)

            # Don't overwrite if new router already has a plan with this name
            if HotspotPlan.objects.filter(router=instance, name=plan.name).exists():
                continue

            HotspotPlan.objects.create(
                router=instance,
                name=plan.name,
                description=plan.description,
                price=plan.price,
                currency=plan.currency,
                validity_type=plan.validity_type,
                validity_value=plan.validity_value,
                duration_minutes=plan.duration_minutes,
                limitation_type=plan.limitation_type,
                data_limit_value=plan.data_limit_value,
                data_limit_unit=plan.data_limit_unit,
                data_limit_mb=plan.data_limit_mb,
                download_speed=plan.download_speed,
                upload_speed=plan.upload_speed,
                speed_unit=plan.speed_unit,
                speed_limit_mbps=plan.speed_limit_mbps,
                simultaneous_devices=plan.simultaneous_devices,
                valid_monday=plan.valid_monday,
                valid_tuesday=plan.valid_tuesday,
                valid_wednesday=plan.valid_wednesday,
                valid_thursday=plan.valid_thursday,
                valid_friday=plan.valid_friday,
                valid_saturday=plan.valid_saturday,
                valid_sunday=plan.valid_sunday,
                is_active=plan.is_active,
                is_popular=plan.is_popular,
                sort_order=plan.sort_order,
                is_global_template=False,  # Clones are NOT templates themselves
                created_by=plan.created_by,
            )
            plans_cloned += 1

        if plans_cloned:
            logger.info(
                f"Auto-cloned {plans_cloned} global hotspot plan(s) "
                f"to new router '{instance.name}' (id={instance.id})"
            )

    except Exception as e:
        logger.error(
            f"Failed to clone global hotspot plans to router {instance.id}: {e}",
            exc_info=True,
        )