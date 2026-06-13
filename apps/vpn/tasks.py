"""
VPN Celery Tasks — Cloud Controller VPN Health Monitoring

Periodic tasks for:
- Monitoring VPN tunnel status for all provisioned routers
- Re-provisioning disconnected routers
- Cleaning up orphaned peer configurations

NOTE: network/vpn models live in TENANT_APPS, so any query against
Router or VpnCertificate must run inside a tenant schema_context.
"""

import logging
from celery import shared_task

from django.utils import timezone
from django_tenants.utils import get_tenant_model, schema_context

logger = logging.getLogger(__name__)


def _for_each_tenant(callback):
    """Run callback(tenant) inside every active tenant's schema."""
    from django_tenants.utils import schema_context
    from apps.core.models import Tenant

    totals = {}
    for tenant in Tenant.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                result = callback(tenant) or {}
                for k, v in result.items():
                    if isinstance(v, (int, float)):
                        totals[k] = totals.get(k, 0) + v
        except Exception as e:
            logger.error(
                f"VPN task error in tenant '{tenant.schema_name}': {e}",
                exc_info=True,
            )
    return totals


@shared_task(name='apps.vpn.tasks.monitor_vpn_tunnels')
def monitor_vpn_tunnels():
    """
    Periodic task: Check WireGuard tunnel status for all provisioned routers.
    
    Connects to the WireGuard server and compares connected peers against
    provisioned routers. Logs disconnected routers and optionally fires alerts.
    
    Runs every 2 minutes via Celery Beat.
    """
    import time
    from apps.vpn.services.wireguard_manager import list_connected_peers
    from apps.network.models.router_models import Router
    from django_tenants.utils import schema_context, get_tenant_model
    from django.utils import timezone
    import logging

    logger = logging.getLogger(__name__)

    try:
        connected_peers = list_connected_peers()
        now = time.time()
        connected_pubkeys = {
            p['public_key']
            for p in connected_peers
            if (now - p['latest_handshake']) < 180  # Active within last 3 minutes
        }
    except Exception as e:
        logger.error(f"[WG MONITOR] Cannot read peers: {e}")
        return {'error': str(e)}

    TenantModel = get_tenant_model()
    tenants = TenantModel.objects.exclude(schema_name='public')
    connected_count    = 0
    disconnected_count = 0
    total_provisioned  = 0

    for tenant in tenants:
        with schema_context(tenant.schema_name):
            routers = Router.objects.filter(vpn_provisioned=True, is_active=True)
            total_provisioned += routers.count()
            for router in routers:
                if router.wireguard_public_key and router.wireguard_public_key in connected_pubkeys:
                    connected_count += 1
                    # Update last seen if more than 60 seconds ago
                    if not router.vpn_last_seen or (timezone.now() - router.vpn_last_seen).total_seconds() > 60:
                        Router.objects.filter(id=router.id).update(vpn_last_seen=timezone.now())
                else:
                    disconnected_count += 1

    return {
        'total_provisioned': total_provisioned,
        'connected':         connected_count,
        'disconnected':      disconnected_count,
        'wg_peers_total':    len(connected_peers),
    }


@shared_task(name='apps.vpn.tasks.check_vpn_health')
def check_vpn_health():
    """
    Periodic task: Quick health check of the WireGuard VPN server.
    
    Checks if the WireGuard interface is active and returns basic stats.
    Runs every minute — lightweight check.
    """
    try:
        from apps.vpn.services.wireguard_manager import get_wireguard_interface_stats
        
        stats = get_wireguard_interface_stats()
        
        if stats and stats.get('status') == 'active':
            return {
                'status': 'healthy',
                'stats': stats,
            }
        else:
            logger.error("WireGuard interface not responding")
            return {'status': 'unhealthy', 'error': 'interface down or not responding'}
            
    except Exception as e:
        logger.error(f"VPN health check failed: {e}")
        return {'status': 'error', 'error': str(e)}


@shared_task(name='apps.vpn.tasks.cleanup_orphaned_peers')
def cleanup_orphaned_peers():
    """
    Periodic task: Remove WireGuard peer configurations for routers that no longer exist.
    
    Runs daily — compares WireGuard server peer list with provisioned routers.
    """
    try:
        from apps.vpn.services.wireguard_manager import (
            list_connected_peers, remove_peer_by_public_key
        )
        from apps.network.models.router_models import Router

        # Get all active peer public keys from the WireGuard server
        try:
            all_peers = list_connected_peers()  # Returns all configured peers
            server_peer_keys = {p['public_key'] for p in all_peers}
        except Exception as e:
            logger.error(f"Failed to list WireGuard peers: {e}")
            return {'error': str(e)}

        if not server_peer_keys:
            return {'orphaned_removed': 0}

        # Collect all provisioned router public keys across every tenant
        all_provisioned_keys: set = set()

        def _collect_keys(tenant):
            from apps.network.models.router_models import Router
            keys = set(
                Router.objects.filter(
                    vpn_provisioned=True,
                    wireguard_public_key__isnull=False,
                ).values_list('wireguard_public_key', flat=True)
            )
            all_provisioned_keys.update(keys)
            return {}

        _for_each_tenant(_collect_keys)

        # Find and remove orphaned peers
        orphaned = 0
        for pubkey in server_peer_keys:
            if pubkey not in all_provisioned_keys:
                if remove_peer_by_public_key(pubkey):
                    orphaned += 1
                    logger.info(f"Removed orphaned WireGuard peer: {pubkey[:16]}...")
                else:
                    logger.warning(f"Failed to remove orphaned peer: {pubkey[:16]}...")

        if orphaned:
            logger.info(f"Cleaned up {orphaned} orphaned WireGuard peers")

        return {'orphaned_removed': orphaned}

    except Exception as e:
        logger.error(f"WireGuard peer cleanup task failed: {e}", exc_info=True)
        return {'error': str(e)}


@shared_task(name='apps.vpn.tasks.cleanup_orphaned_ccd')
def cleanup_orphaned_ccd():
    """
    Legacy task: Keep for backward compatibility but log deprecation warning.
    
    CCD files are no longer used with WireGuard. This task is maintained
    for systems that still have OpenVPN fallback routers.
    """
    logger.warning(
        "cleanup_orphaned_ccd called but WireGuard doesn't use CCD files. "
        "Use cleanup_orphaned_peers instead."
    )
    return {
        'warning': 'CCD cleanup not applicable for WireGuard',
        'orphaned_removed': 0
    }


