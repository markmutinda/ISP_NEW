"""
VPN Celery Tasks — Cloud Controller VPN Health Monitoring

Periodic tasks for:
- Monitoring VPN tunnel status for all provisioned routers
- Re-provisioning disconnected routers
- Cleaning up orphaned CCD files
"""

import logging
from celery import shared_task

from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name='apps.vpn.tasks.monitor_vpn_tunnels')
def monitor_vpn_tunnels():
    """
    Periodic task: Check VPN tunnel status for all provisioned routers.
    
    Connects to the OpenVPN management interface and compares connected
    clients against provisioned routers. Logs disconnected routers and
    optionally fires alerts.
    
    Runs every 2 minutes via Celery Beat.
    """
    try:
        from apps.vpn.services.openvpn_management import OpenVPNManagementClient
        from apps.network.models.router_models import Router
        
        client = OpenVPNManagementClient()
        
        # Get currently connected VPN clients
        try:
            connected_clients = client.get_connected_clients()
            connected_cns = {c.common_name for c in connected_clients}
        except Exception as e:
            logger.error(f"Cannot reach OpenVPN management interface: {e}")
            return {'error': f'OpenVPN unreachable: {e}'}
        
        # Get all provisioned routers
        provisioned_routers = Router.objects.filter(
            vpn_provisioned=True,
            is_active=True
        ).select_related('vpn_certificate')
        
        connected_count = 0
        disconnected_count = 0
        disconnected_routers = []
        
        for router in provisioned_routers:
            # The CN used in the VPN cert
            expected_cn = None
            if router.vpn_certificate:
                expected_cn = router.vpn_certificate.common_name
            
            if expected_cn and expected_cn in connected_cns:
                connected_count += 1
                # Update last seen timestamp
                if not router.vpn_last_seen or (
                    timezone.now() - router.vpn_last_seen
                ).total_seconds() > 60:
                    Router.objects.filter(id=router.id).update(
                        vpn_last_seen=timezone.now()
                    )
            else:
                disconnected_count += 1
                disconnected_routers.append({
                    'id': router.id,
                    'name': router.name,
                    'vpn_ip': str(router.vpn_ip_address) if router.vpn_ip_address else None,
                })
        
        result = {
            'total_provisioned': provisioned_routers.count(),
            'connected': connected_count,
            'disconnected': disconnected_count,
            'vpn_clients_total': len(connected_clients),
        }
        
        if disconnected_count:
            logger.warning(
                f"VPN Monitor: {disconnected_count} routers disconnected: "
                f"{[r['name'] for r in disconnected_routers[:5]]}"
            )
        
        return result
        
    except Exception as e:
        logger.error(f"VPN monitoring task failed: {e}", exc_info=True)
        return {'error': str(e)}


@shared_task(name='apps.vpn.tasks.check_vpn_health')
def check_vpn_health():
    """
    Periodic task: Quick health check of the OpenVPN server.
    
    Pings the management interface and logs basic stats.
    Runs every minute — lightweight check.
    """
    try:
        from apps.vpn.services.openvpn_management import OpenVPNManagementClient
        
        client = OpenVPNManagementClient()
        
        if client.ping():
            stats = client.get_server_stats()
            return {
                'status': 'healthy',
                'stats': stats,
            }
        else:
            logger.error("OpenVPN management interface not responding")
            return {'status': 'unhealthy', 'error': 'ping failed'}
            
    except Exception as e:
        logger.error(f"VPN health check failed: {e}")
        return {'status': 'error', 'error': str(e)}


@shared_task(name='apps.vpn.tasks.cleanup_orphaned_ccd')
def cleanup_orphaned_ccd():
    """
    Periodic task: Remove CCD files for routers that no longer exist.
    
    Runs daily — compares CCD directory with provisioned routers.
    """
    try:
        from apps.vpn.services.ccd_manager import CCDManager
        from apps.network.models.router_models import Router
        
        manager = CCDManager()
        ccd_files = manager.list_ccd_files()
        
        if not ccd_files:
            return {'orphaned_removed': 0}
        
        # Get all provisioned router CNs
        provisioned_cns = set(
            Router.objects.filter(
                vpn_provisioned=True,
                vpn_certificate__isnull=False
            ).values_list('vpn_certificate__common_name', flat=True)
        )
        
        orphaned = 0
        for ccd_file in ccd_files:
            if ccd_file not in provisioned_cns:
                manager.remove_ccd_file(ccd_file)
                orphaned += 1
                logger.info(f"Removed orphaned CCD file: {ccd_file}")
        
        if orphaned:
            logger.info(f"Cleaned up {orphaned} orphaned CCD files")
        
        return {'orphaned_removed': orphaned}
        
    except Exception as e:
        logger.error(f"CCD cleanup task failed: {e}", exc_info=True)
        return {'error': str(e)}
