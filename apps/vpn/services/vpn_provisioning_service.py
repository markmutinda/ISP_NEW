"""
VPN Provisioning Service — Dual Stack (OpenVPN v6 + WireGuard v7)

OpenVPN  → 10.8.0.0/16 range → existing clients unaffected
WireGuard → 10.9.0.0/16 range → new v7 clients

Both tunnel to same VPS, both hit same FreeRADIUS server,
both managed from same Django dashboard.

Reference architecture: Centipid Technologies dual-VPN approach
"""

import ipaddress
import logging
import subprocess
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import schema_context, get_public_schema_name

logger = logging.getLogger(__name__)


class VPNProvisioningError(Exception):
    pass


class VPNProvisioningService:

    # Separate IP ranges — never overlap
    OPENVPN_NETWORK = getattr(settings, 'OPENVPN_NETWORK_CIDR', '10.8.0.0/16')
    OPENVPN_SERVER_IP = getattr(settings, 'OPENVPN_SERVER_IP', '10.8.0.1')

    WIREGUARD_NETWORK = getattr(settings, 'WG_NETWORK_CIDR', '10.9.0.0/16')
    WIREGUARD_SERVER_IP = getattr(settings, 'WG_SERVER_IP', '10.9.0.1')

    def __init__(self):
        # Only import CertificateService if cryptography is available
        # WireGuard doesn't need it
        try:
            from apps.vpn.services.certificate_service import CertificateService
            self.cert_service = CertificateService()
        except ImportError:
            self.cert_service = None

        from apps.vpn.services.ccd_manager import CCDManager
        self.ccd_manager = CCDManager()

    def provision_router(self, router) -> dict:
        """
        Main entry point — called exactly the same way for all routers.
        Detects correct VPN type from routeros_version and provisions accordingly.

        Existing OpenVPN routers are NEVER touched by this logic unless
        they explicitly re-run the script on v7 hardware.
        """
        ros_version = getattr(router, 'routeros_version', None)

        # If router already has a vpn_type set and is provisioned,
        # respect it — don't reprovision automatically
        if router.vpn_provisioned and router.vpn_ip_address:
            logger.info(
                f"Router {router.name} already provisioned as "
                f"{router.vpn_type} with IP {router.vpn_ip_address} — skipping"
            )
            return {
                'vpn_ip': router.vpn_ip_address,
                'type': router.vpn_type,
                'status': 'already_provisioned'
            }

        # Determine VPN type from RouterOS version
        if ros_version and str(ros_version).startswith('6'):
            logger.info(
                f"Router {router.name} is ROS v6 — "
                f"provisioning OpenVPN (10.8.x.x range)"
            )
            return self._provision_openvpn(router)
        else:
            logger.info(
                f"Router {router.name} is ROS v7 (or unknown) — "
                f"provisioning WireGuard (10.9.x.x range)"
            )
            return self._provision_wireguard(router)

    def reprovision_router(self, router) -> dict:
        """
        Explicit reprovision — called when admin clicks reprovision
        or when a v6 client upgrades to v7 and re-runs the script.
        This WILL change VPN type if the version changed.
        """
        logger.info(f"Reprovisioning router {router.name} (current type: {router.vpn_type})")
        self.deprovision_router(router)
        # Clear vpn_provisioned so provision_router picks the right type fresh
        router.vpn_provisioned = False
        router.vpn_ip_address = None
        router.save(update_fields=['vpn_provisioned', 'vpn_ip_address'])
        return self.provision_router(router)

    def deprovision_router(self, router) -> None:
        """Clean removal — handles both VPN types correctly"""
        if router.vpn_type == 'wireguard':
            self._deprovision_wireguard(router)
        else:
            self._deprovision_openvpn(router)

        # Clear GlobalRouterMap entry regardless of type
        self._unregister_router_globally(router)

        # Clear all VPN fields
        router.vpn_type = 'openvpn'  # reset to default
        router.vpn_ip_address = None
        router.vpn_provisioned = False
        router.vpn_provisioned_at = None
        router.wg_private_key = None
        router.wg_public_key = None
        router.ca_certificate = None
        router.client_certificate = None
        router.client_key = None
        router.save(update_fields=[
            'vpn_type', 'vpn_ip_address', 'vpn_provisioned',
            'vpn_provisioned_at', 'wg_private_key', 'wg_public_key',
            'ca_certificate', 'client_certificate', 'client_key', 'updated_at'
        ])

    # ─────────────────────────────────────────────────────────────
    # WIREGUARD (v7)
    # ─────────────────────────────────────────────────────────────

    def _provision_wireguard(self, router) -> dict:
        """
        Provision WireGuard tunnel for RouterOS v7 router.
        Server-side keypair generation (same approach as Centipid).
        """
        try:
            with transaction.atomic():
                # 1. Generate keypair server-side
                private_key, public_key = self._generate_wg_keypair()
                logger.info(f"Generated WireGuard keypair for {router.name}")

                # 2. Assign IP from WireGuard range (10.9.x.x)
                vpn_ip = self._assign_ip(self.WIREGUARD_NETWORK, 'wireguard')
                logger.info(f"Assigned WireGuard IP {vpn_ip} to {router.name}")

                # 3. Add peer to WireGuard server
                self._add_wg_peer(public_key, vpn_ip)
                logger.info(f"Added WireGuard peer {public_key[:8]}... for {router.name}")

                # 4. Save to router model
                router.vpn_type = 'wireguard'
                router.wg_private_key = private_key
                router.wg_public_key = public_key
                router.vpn_ip_address = vpn_ip
                router.ip_address = vpn_ip
                router.vpn_provisioned = True
                router.vpn_provisioned_at = timezone.now()
                # Clear OpenVPN cert fields — not needed for WireGuard
                router.ca_certificate = None
                router.client_certificate = None
                router.client_key = None
                router.save(update_fields=[
                    'vpn_type', 'wg_private_key', 'wg_public_key',
                    'vpn_ip_address', 'ip_address', 'vpn_provisioned',
                    'vpn_provisioned_at', 'ca_certificate',
                    'client_certificate', 'client_key', 'updated_at'
                ])

                # 5. Register in GlobalRouterMap (public schema)
                self._register_router_globally(router, vpn_ip)

                logger.info(
                    f"WireGuard provisioning complete for {router.name}: "
                    f"IP={vpn_ip}"
                )
                return {
                    'vpn_ip': vpn_ip,
                    'type': 'wireguard',
                    'status': 'provisioned'
                }

        except Exception as e:
            logger.error(
                f"WireGuard provisioning failed for {router.name}: {e}",
                exc_info=True
            )
            raise VPNProvisioningError(
                f"WireGuard provisioning failed for {router.name}: {e}"
            )

    def _deprovision_wireguard(self, router) -> None:
        """Remove WireGuard peer from server"""
        if not router.wg_public_key:
            logger.warning(f"Router {router.name} has no WireGuard public key to remove")
            return

        try:
            subprocess.run(
                ['wg', 'set', 'wg0', 'peer', router.wg_public_key, '--remove'],
                check=True,
                capture_output=True
            )
            subprocess.run(
                ['wg-quick', 'save', 'wg0'],
                check=True,
                capture_output=True
            )
            logger.info(f"Removed WireGuard peer for {router.name}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to remove WireGuard peer for {router.name}: {e}")
        except FileNotFoundError:
            logger.warning(
                "wg command not found — WireGuard may not be installed on this server. "
                "Peer not removed from server config."
            )

    def _generate_wg_keypair(self):
        """Generate WireGuard keypair using wg command"""
        try:
            private_key = subprocess.check_output(
                ['wg', 'genkey'],
                text=True,
                stderr=subprocess.PIPE
            ).strip()

            public_key = subprocess.check_output(
                ['wg', 'pubkey'],
                input=private_key,
                text=True,
                stderr=subprocess.PIPE
            ).strip()

            return private_key, public_key

        except FileNotFoundError:
            raise VPNProvisioningError(
                "wg command not found. Install WireGuard on the server: "
                "apt-get install wireguard-tools"
            )
        except subprocess.CalledProcessError as e:
            raise VPNProvisioningError(f"WireGuard keygen failed: {e}")

    def _add_wg_peer(self, public_key: str, vpn_ip: str) -> None:
        """Add a peer to the running WireGuard interface on the server"""
        try:
            subprocess.run(
                [
                    'wg', 'set', 'wg0',
                    'peer', public_key,
                    'allowed-ips', f'{vpn_ip}/32'
                ],
                check=True,
                capture_output=True
            )
            # Persist so peers survive server reboot
            subprocess.run(
                ['wg-quick', 'save', 'wg0'],
                check=True,
                capture_output=True
            )
        except FileNotFoundError:
            raise VPNProvisioningError(
                "wg command not found. WireGuard not installed on server."
            )
        except subprocess.CalledProcessError as e:
            raise VPNProvisioningError(f"Failed to add WireGuard peer: {e}")

    # ─────────────────────────────────────────────────────────────
    # OPENVPN (v6) — existing logic preserved exactly
    # ─────────────────────────────────────────────────────────────

    def _provision_openvpn(self, router) -> dict:
        """
        Provision OpenVPN for RouterOS v6 router.
        This is your existing logic — untouched so existing clients
        are completely unaffected.
        """
        try:
            with transaction.atomic():
                # 1. Get or create CA
                ca = self._ensure_ca()

                # 2. Assign IP from OpenVPN range (10.8.x.x)
                vpn_ip = self._assign_ip(self.OPENVPN_NETWORK, 'openvpn')
                logger.info(f"Assigned OpenVPN IP {vpn_ip} to {router.name}")

                # 3. Generate certificate CN from openvpn_username
                common_name = self._generate_cn(router)

                # 4. Generate client certificate
                cert_record = self._generate_client_certificate(ca, router, common_name)

                # 5. Write CCD file
                self.ccd_manager.create_ccd_file(common_name, vpn_ip)
                logger.info(f"CCD file written for {common_name} → {vpn_ip}")

                # 6. Save to router
                router.vpn_type = 'openvpn'
                router.vpn_ip_address = vpn_ip
                router.ip_address = vpn_ip
                router.vpn_certificate = cert_record
                router.ca_certificate = ca.ca_certificate
                router.client_certificate = cert_record.certificate
                router.client_key = cert_record.private_key
                router.vpn_provisioned = True
                router.vpn_provisioned_at = timezone.now()
                router.save(update_fields=[
                    'vpn_type', 'vpn_ip_address', 'ip_address',
                    'vpn_certificate', 'ca_certificate',
                    'client_certificate', 'client_key',
                    'vpn_provisioned', 'vpn_provisioned_at', 'updated_at'
                ])

                # 7. Register in GlobalRouterMap
                self._register_router_globally(router, vpn_ip)

                logger.info(
                    f"OpenVPN provisioning complete for {router.name}: "
                    f"IP={vpn_ip}"
                )
                return {
                    'vpn_ip': vpn_ip,
                    'type': 'openvpn',
                    'status': 'provisioned'
                }

        except Exception as e:
            logger.error(
                f"OpenVPN provisioning failed for {router.name}: {e}",
                exc_info=True
            )
            raise VPNProvisioningError(
                f"OpenVPN provisioning failed for {router.name}: {e}"
            )

    def _deprovision_openvpn(self, router) -> None:
        """Remove OpenVPN CCD file and revoke certificate"""
        # Revoke certificate
        if router.vpn_certificate:
            try:
                router.vpn_certificate.revoke(
                    reason=f"Router {router.name} deprovisioned"
                )
            except Exception as e:
                logger.error(f"Failed to revoke cert for {router.name}: {e}")

        # Remove CCD file
        common_name = self._generate_cn(router)
        try:
            self.ccd_manager.remove_ccd_file(common_name)
        except Exception as e:
            logger.error(f"Failed to remove CCD for {router.name}: {e}")

    # ─────────────────────────────────────────────────────────────
    # SHARED HELPERS
    # ─────────────────────────────────────────────────────────────

    def _assign_ip(self, network_cidr: str, vpn_type: str) -> str:
        """
        Assign next available IP from the correct pool.
        Queries ONLY routers of matching vpn_type.
        OpenVPN pool (10.8.x.x) and WireGuard pool (10.9.x.x)
        never interfere with each other.
        """
        from apps.network.models.router_models import Router
        from apps.core.models import GlobalRouterMap
        from django_tenants.utils import schema_context, get_public_schema_name

        network = ipaddress.ip_network(network_cidr, strict=False)

        # Get all IPs already assigned globally for this VPN type
        with schema_context(get_public_schema_name()):
            # Check GlobalRouterMap for the network range
            all_map_ips = set(
                GlobalRouterMap.objects.values_list('nas_ip', flat=True)
            )

        # Filter to only IPs in this specific network range
        used_ips = set()
        for ip_str in all_map_ips:
            if ip_str:
                try:
                    ip = ipaddress.ip_address(ip_str)
                    if ip in network:
                        used_ips.add(ip)
                except ValueError:
                    pass

        # Reserved: first IP is network gateway (.0.1)
        reserved = {network.network_address + 1}

        # Find first available
        for host in network.hosts():
            if host not in reserved and host not in used_ips:
                return str(host)

        raise VPNProvisioningError(
            f"No available IPs in {network_cidr} for {vpn_type}. "
            f"Used: {len(used_ips)}, Capacity: {network.num_addresses - 2}"
        )

    def _ensure_ca(self):
        """Get active CA for OpenVPN — unchanged from your existing code"""
        from apps.vpn.models import CertificateAuthority

        ca = CertificateAuthority.objects.filter(is_active=True).first()
        if ca:
            return ca

        logger.info("No active CA — creating new Certificate Authority")
        ca = self.cert_service.create_ca(
            name="Netily Cloud CA",
            common_name="Netily Cloud Controller CA",
            organization="Netily ISP Platform",
            country="KE",
        )
        return ca

    def _generate_cn(self, router) -> str:
        """Generate OpenVPN certificate CN from router username"""
        if not router.openvpn_username:
            raise VPNProvisioningError(
                f"Router {router.name} has no openvpn_username"
            )
        return router.openvpn_username

    def _generate_client_certificate(self, ca, router, common_name: str):
        """Generate OpenVPN client cert — unchanged from your existing code"""
        from apps.vpn.models import VPNCertificate

        existing = VPNCertificate.objects.filter(
            router=router,
            status='active',
            certificate_type='client',
        ).first()

        if existing:
            existing.revoke(reason="Replaced by new provisioning")

        return self.cert_service.generate_client_certificate(
            ca=ca,
            router=router,
            common_name=common_name,
        )

    def _register_router_globally(self, router, vpn_ip: str) -> None:
        """
        Register router in GlobalRouterMap (public schema).
        Works identically for both VPN types — the IP range
        tells FreeRADIUS which network the packet came from.
        """
        from apps.core.models import GlobalRouterMap, Tenant

        tenant_schema = getattr(router, 'schema_name', None)
        tenant_subdomain = getattr(router, 'tenant_subdomain', None)

        with schema_context(get_public_schema_name()):
            tenant = None
            if tenant_schema:
                tenant = Tenant.objects.filter(
                    schema_name=tenant_schema
                ).first()
            if not tenant and tenant_subdomain:
                tenant = Tenant.objects.filter(
                    subdomain=tenant_subdomain
                ).first()

            if not tenant:
                logger.error(
                    f"Cannot register {router.name} in GlobalRouterMap: "
                    f"tenant not found (schema={tenant_schema})"
                )
                return

            GlobalRouterMap.objects.update_or_create(
                nas_ip=vpn_ip,
                defaults={
                    'nas_secret': router.shared_secret or 'default_secret',
                    'tenant': tenant,
                    'is_active': True,
                }
            )
            logger.info(
                f"GlobalRouterMap: {vpn_ip} → {tenant.schema_name} "
                f"({router.vpn_type})"
            )

    def _unregister_router_globally(self, router) -> None:
        """Remove from GlobalRouterMap when router deleted"""
        from apps.core.models import GlobalRouterMap

        if not router.vpn_ip_address:
            return

        with schema_context(get_public_schema_name()):
            deleted, _ = GlobalRouterMap.objects.filter(
                nas_ip=router.vpn_ip_address
            ).delete()

            if deleted:
                logger.info(
                    f"Removed GlobalRouterMap entry for "
                    f"{router.name} ({router.vpn_ip_address})"
                )