"""
VPN Celery Tasks — Cloud Controller VPN Health Monitoring

Periodic tasks for:
- Monitoring VPN tunnel status for all provisioned routers
- Re-provisioning disconnected routers
- Cleaning up orphaned CCD files

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
        try:
            connected_clients = client.get_connected_clients()
            connected_cns = {c.common_name for c in connected_clients}
        except Exception as e:
            logger.error(f"Cannot reach OpenVPN management interface: {e}")
            return {'error': f'OpenVPN unreachable: {e}'}

        TenantModel = get_tenant_model()
        tenants = TenantModel.objects.exclude(schema_name='public')

        connected_count = 0
        disconnected_count = 0
        total_provisioned = 0

        for tenant in tenants:
            with schema_context(tenant.schema_name):
                provisioned_routers = Router.objects.filter(
                    vpn_provisioned=True,
                    is_active=True
                ).select_related('vpn_certificate')

                total_provisioned += provisioned_routers.count()

                for router in provisioned_routers:
                    expected_cn = router.vpn_certificate.common_name if router.vpn_certificate else None
                    if expected_cn and expected_cn in connected_cns:
                        connected_count += 1
                        if not router.vpn_last_seen or (timezone.now() - router.vpn_last_seen).total_seconds() > 60:
                            Router.objects.filter(id=router.id).update(vpn_last_seen=timezone.now())
                    else:
                        disconnected_count += 1

        return {
            'total_provisioned': total_provisioned,
            'connected': connected_count,
            'disconnected': disconnected_count,
            'vpn_clients_total': len(connected_clients),
        }

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

        manager = CCDManager()
        ccd_files = manager.list_ccd_files()

        if not ccd_files:
            return {'orphaned_removed': 0}

        # Collect all provisioned CNs across every tenant
        all_provisioned_cns: set = set()

        def _collect_cns(tenant):
            from apps.network.models.router_models import Router
            cns = set(
                Router.objects.filter(
                    vpn_provisioned=True,
                    vpn_certificate__isnull=False,
                ).values_list('vpn_certificate__common_name', flat=True)
            )
            all_provisioned_cns.update(cns)
            return {}

        _for_each_tenant(_collect_cns)

        orphaned = 0
        for ccd_file in ccd_files:
            if ccd_file not in all_provisioned_cns:
                manager.remove_ccd_file(ccd_file)
                orphaned += 1
                logger.info(f"Removed orphaned CCD file: {ccd_file}")

        if orphaned:
            logger.info(f"Cleaned up {orphaned} orphaned CCD files")

        return {'orphaned_removed': orphaned}

    except Exception as e:
        logger.error(f"CCD cleanup task failed: {e}", exc_info=True)
        return {'error': str(e)}