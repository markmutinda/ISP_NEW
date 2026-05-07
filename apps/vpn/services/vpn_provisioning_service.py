"""
VPN Provisioning Service — WireGuard (replaces OpenVPN)
"""
import ipaddress
import logging
from typing import Set

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import schema_context, get_public_schema_name

from apps.vpn.services.wireguard_manager import (
    generate_keypair, add_peer, remove_peer, get_server_public_key
)
from apps.core.models import GlobalRouterMap

logger = logging.getLogger(__name__)


class VPNProvisioningError(Exception):
    pass


class VPNProvisioningService:

    def provision_router(self, router) -> dict:
        logger.info(f"[WG PROVISION] Starting for router: {router.name} (id={router.id})")

        try:
            with transaction.atomic():
                # 1. Assign globally unique VPN IP
                vpn_ip = self._assign_vpn_ip(router)

                # 2. Generate WireGuard keypair for this router
                keypair = generate_keypair()

                # 3. Register peer on the WireGuard server
                add_peer(keypair['public_key'], vpn_ip)

                # 4. Update router record
                router.wireguard_private_key = keypair['private_key']
                router.wireguard_public_key  = keypair['public_key']
                router.vpn_ip_address        = vpn_ip
                router.vpn_provisioned       = True
                router.vpn_provisioned_at    = timezone.now()
                router.ip_address            = vpn_ip  # management IP
                router.status                = 'online'
                router.save(update_fields=[
                    'wireguard_private_key', 'wireguard_public_key',
                    'vpn_ip_address', 'vpn_provisioned', 'vpn_provisioned_at',
                    'ip_address', 'status', 'updated_at',
                ])

                # 5. Register in GlobalRouterMap (public schema)
                self._register_router_globally(router, vpn_ip)

                result = {
                    'vpn_ip':     vpn_ip,
                    'public_key': keypair['public_key'],
                    'status':     'provisioned',
                }
                logger.info(f"[WG PROVISION] Complete for {router.name}: {result}")
                return result

        except Exception as e:
            logger.error(f"[WG PROVISION] Failed for {router.name}: {e}", exc_info=True)
            raise VPNProvisioningError(str(e))

    def deprovision_router(self, router) -> None:
        if router.wireguard_public_key:
            remove_peer(router.wireguard_public_key)

        self._unregister_router_globally(router)

        router.wireguard_private_key = None
        router.wireguard_public_key  = None
        router.vpn_ip_address        = None
        router.vpn_provisioned       = False
        router.vpn_provisioned_at    = None
        router.save(update_fields=[
            'wireguard_private_key', 'wireguard_public_key',
            'vpn_ip_address', 'vpn_provisioned', 'vpn_provisioned_at', 'updated_at',
        ])

    def reprovision_router(self, router) -> dict:
        self.deprovision_router(router)
        return self.provision_router(router)

    # ── Private helpers ──────────────────────────────────────────────────────

    def _assign_vpn_ip(self, router) -> str:
        if router.vpn_ip_address:
            return str(router.vpn_ip_address)

        vpn_cidr = getattr(settings, 'VPN_NETWORK_CIDR', '10.8.0.0/16')
        try:
            vpn_net = ipaddress.ip_network(vpn_cidr, strict=False)
        except ValueError as e:
            raise VPNProvisioningError(f"Invalid VPN_NETWORK_CIDR: {e}")

        reserved_str = getattr(settings, 'VPN_RESERVED_IPS', '10.8.0.1,10.8.0.2')
        reserved: Set[ipaddress.IPv4Address] = set()
        for ip_str in reserved_str.split(','):
            ip_str = ip_str.strip()
            if ip_str:
                try:
                    reserved.add(ipaddress.ip_address(ip_str))
                except ValueError:
                    pass

        with schema_context(get_public_schema_name()):
            assigned: Set[ipaddress.IPv4Address] = set()
            for ip_str in GlobalRouterMap.objects.values_list('nas_ip', flat=True):
                if ip_str:
                    try:
                        assigned.add(ipaddress.ip_address(ip_str))
                    except ValueError:
                        pass

        for host in vpn_net.hosts():
            if host in reserved or host in assigned:
                continue
            return str(host)

        raise VPNProvisioningError(f"No available VPN IPs in {vpn_cidr}")

    def _register_router_globally(self, router, vpn_ip: str) -> None:
        from apps.core.models import Tenant
        tenant_schema = getattr(router, 'schema_name', None)
        tenant_subdomain = getattr(router, 'tenant_subdomain', None)

        with schema_context(get_public_schema_name()):
            tenant = None
            if tenant_schema:
                tenant = Tenant.objects.filter(schema_name=tenant_schema).first()
            if not tenant and tenant_subdomain:
                tenant = Tenant.objects.filter(subdomain=tenant_subdomain).first()
            if not tenant:
                logger.error(f"[WG] Cannot register {router.name}: tenant not found")
                return

            GlobalRouterMap.objects.update_or_create(
                nas_ip=vpn_ip,
                defaults={
                    'nas_secret': router.shared_secret or 'default_secret',
                    'tenant':     tenant,
                    'is_active':  True,
                },
            )
            logger.info(f"[WG] GlobalRouterMap: {vpn_ip} → {tenant.schema_name}")

    def _unregister_router_globally(self, router) -> None:
        if not router.vpn_ip_address:
            return
        with schema_context(get_public_schema_name()):
            GlobalRouterMap.objects.filter(nas_ip=router.vpn_ip_address).delete()