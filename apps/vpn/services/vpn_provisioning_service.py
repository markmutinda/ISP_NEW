"""
VPN Provisioning Service — Cloud Controller Auto-Provisioning

Handles the full lifecycle when a new Router is created:
1. Assigns next available static VPN IP from the 10.8.0.0/24 pool (GLOBALLY unique across all tenants)
2. Generates a client certificate via CertificateService
3. Writes a CCD file mapping the certificate CN → static IP
4. Stores PEM content on the Router model for .rsc script injection
"""

import logging
from typing import Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import schema_context, get_public_schema_name

from apps.vpn.models import CertificateAuthority, VPNCertificate, VPNServer
from apps.vpn.services.certificate_service import CertificateService
from apps.vpn.services.ccd_manager import CCDManager
from apps.core.models import GlobalRouterMap  # ADDED: For global IP tracking

logger = logging.getLogger(__name__)


class VPNProvisioningError(Exception):
    """Raised when VPN provisioning fails."""
    pass


class VPNProvisioningService:
    """
    Orchestrates the complete VPN provisioning flow for a router.
    Called when a new Router is created or when re-provisioning is requested.
    """

    def __init__(self):
        self.cert_service = CertificateService()
        self.ccd_manager = CCDManager()

    def provision_router(self, router) -> dict:
        """
        Full provisioning pipeline:
        1. Ensure a CA exists
        2. Assign a unique VPN IP (GLOBALLY unique across all tenants)
        3. Generate a client certificate
        4. Write the CCD file
        5. Store everything on the Router record
        6. Register the router in GlobalRouterMap (public schema)
        """
        from apps.network.models.router_models import Router  # late import to avoid circular

        logger.info(f"Starting VPN provisioning for router: {router.name} (id={router.id})")

        try:
            with transaction.atomic():
                # 1. Ensure CA exists
                ca = self._ensure_ca()

                # 2. Assign globally unique VPN IP
                vpn_ip = self._assign_vpn_ip(router)
                logger.info(f"Assigned globally unique VPN IP {vpn_ip} to router {router.name}")

                # 3. Generate client certificate
                common_name = self._generate_cn(router)  # ← returns openvpn_username
                cert_record = self._generate_client_certificate(ca, router, common_name)
                logger.info(f"Generated certificate CN={common_name} for router {router.name}")

                # 4. Write CCD file (filename = common_name = username)
                self.ccd_manager.create_ccd_file(common_name, vpn_ip)
                logger.info(f"Wrote CCD file for CN={common_name} → {vpn_ip}")

                # 5. Update Router record
                router.vpn_ip_address = vpn_ip
                router.vpn_certificate = cert_record
                router.ca_certificate = ca.ca_certificate
                router.client_certificate = cert_record.certificate
                router.client_key = cert_record.private_key
                router.vpn_provisioned = True
                router.status = 'online'  # <--- SET STATUS TO ONLINE
                router.vpn_provisioned_at = timezone.now()
                # Also set the management ip_address for backward compat
                router.ip_address = vpn_ip
                router.save(update_fields=[
                    'vpn_ip_address', 'vpn_certificate', 'ca_certificate',
                    'client_certificate', 'client_key', 'vpn_provisioned',
                    'status',           # <--- ADDED STATUS TO UPDATE FIELDS
                    'vpn_provisioned_at', 'ip_address', 'updated_at',
                ])

                # 6. Register router in GlobalRouterMap (public schema)
                self._register_router_globally(router, vpn_ip)

                result = {
                    'vpn_ip': vpn_ip,
                    'common_name': common_name,
                    'certificate_id': str(cert_record.id),
                    'status': 'provisioned',
                }
                logger.info(f"VPN provisioning complete for router {router.name}: {result}")
                return result

        except Exception as e:
            logger.error(f"VPN provisioning failed for router {router.name}: {e}", exc_info=True)
            raise VPNProvisioningError(f"Failed to provision VPN for router {router.name}: {e}")

    def deprovision_router(self, router) -> None:
        """
        Removes VPN provisioning for a router:
        - Revokes the certificate
        - Removes the CCD file
        - Removes from GlobalRouterMap (public schema)
        - Clears Router VPN fields
        """
        logger.info(f"Deprovisioning VPN for router: {router.name}")

        # Revoke certificate
        if router.vpn_certificate:
            router.vpn_certificate.revoke(reason=f"Router {router.name} deprovisioned")

        # Remove CCD (filename = openvpn_username)
        common_name = self._generate_cn(router)
        self.ccd_manager.remove_ccd_file(common_name)

        # Remove from GlobalRouterMap (public schema)
        self._unregister_router_globally(router)

        # Clear fields
        router.vpn_ip_address = None
        router.vpn_certificate = None
        router.ca_certificate = None
        router.client_certificate = None
        router.client_key = None
        router.vpn_provisioned = False
        router.vpn_provisioned_at = None
        router.save(update_fields=[
            'vpn_ip_address', 'vpn_certificate', 'ca_certificate',
            'client_certificate', 'client_key', 'vpn_provisioned',
            'vpn_provisioned_at', 'updated_at',
        ])
        logger.info(f"VPN deprovisioned for router {router.name}")

    def reprovision_router(self, router) -> dict:
        """Deprovision then re-provision (cert rotation, IP change, etc.)."""
        self.deprovision_router(router)
        return self.provision_router(router)

    # ────────────────────────────────────────────────────────────
    # INTERNAL HELPERS
    # ────────────────────────────────────────────────────────────

    def _ensure_ca(self) -> CertificateAuthority:
        """Get the active CA, or create one if none exists."""
        ca = CertificateAuthority.objects.filter(is_active=True).first()
        if ca:
            return ca

        logger.info("No active CA found. Creating a new Certificate Authority...")
        # The CertificateService handles DB creation and returns the CA object
        ca = self.cert_service.create_ca(
            name="Netily Cloud CA",
            common_name="Netily Cloud Controller CA",
            organization="Netily ISP Platform",
            country="KE",
        )
        logger.info(f"Created new CA: {ca.name}")
        return ca

    def _assign_vpn_ip(self, router) -> str:
        """
        Assign globally unique VPN IP across ALL tenants.
        Uses GlobalRouterMap (public schema) as the source of truth.
        
        This prevents duplicate IPs across different tenants because:
        - GlobalRouterMap lives in public/shared space
        - nas_ip field has unique=True constraint
        - Every tenant draws from one common VPN pool
        """
        from django.conf import settings
        from django_tenants.utils import schema_context, get_public_schema_name
        from apps.core.models import GlobalRouterMap

        range_start = getattr(settings, 'VPN_IP_RANGE_START', 10)
        range_end = getattr(settings, 'VPN_IP_RANGE_END', 250)

        # Reuse existing assignment on router
        if router.vpn_ip_address:
            return str(router.vpn_ip_address)

        base = '10.8.0'

        # IMPORTANT: Read globally assigned NAS IPs from PUBLIC schema
        # This ensures we see ALL routers across ALL tenants
        with schema_context(get_public_schema_name()):
            assigned_ips = set(
                GlobalRouterMap.objects.values_list('nas_ip', flat=True)
            )
            logger.debug(f"Currently assigned global VPN IPs: {assigned_ips}")

        # Find first available IP not in global assignments
        for i in range(range_start, range_end + 1):
            candidate = f"{base}.{i}"
            if candidate not in assigned_ips:
                logger.info(f"Found available VPN IP: {candidate}")
                return candidate

        raise VPNProvisioningError(
            f"No available VPN IPs in range {base}.{range_start}-{base}.{range_end}. "
            f"{len(assigned_ips)} IPs already assigned globally."
        )

    def _register_router_globally(self, router, vpn_ip: str) -> None:
        """
        Register the router in the GlobalRouterMap (public schema).
        This makes the IP visible to all tenants for global uniqueness.
        """
        from django_tenants.utils import schema_context, get_public_schema_name
        from apps.core.models import GlobalRouterMap, Tenant

        logger.info(f"Registering router {router.name} (IP: {vpn_ip}) in GlobalRouterMap")

        with schema_context(get_public_schema_name()):
            # Get the tenant that owns this router
            # Since we're in the tenant's schema when this is called,
            # we need to get the tenant reference from the public schema
            tenant = None
            try:
                # The current schema is the tenant's schema
                current_schema = router._state.db if hasattr(router, '_state') else None
                if current_schema:
                    tenant = Tenant.objects.filter(schema_name=current_schema).first()
            except Exception as e:
                logger.warning(f"Could not resolve tenant for router {router.name}: {e}")

            if not tenant:
                # Fallback: try to get tenant by subdomain from router's tenant_subdomain field
                if hasattr(router, 'tenant_subdomain') and router.tenant_subdomain:
                    tenant = Tenant.objects.filter(subdomain=router.tenant_subdomain).first()

            if not tenant:
                logger.error(f"Cannot register router {router.name}: No tenant found")
                return

            # Create or update GlobalRouterMap entry
            global_entry, created = GlobalRouterMap.objects.update_or_create(
                nas_ip=vpn_ip,
                defaults={
                    'nas_secret': router.radius_secret or 'default_secret',
                    'tenant': tenant,
                    'is_active': True,
                }
            )
            
            if created:
                logger.info(f"Created GlobalRouterMap entry: {vpn_ip} -> {tenant.schema_name}")
            else:
                logger.info(f"Updated GlobalRouterMap entry: {vpn_ip} -> {tenant.schema_name}")

    def _unregister_router_globally(self, router) -> None:
        """
        Remove the router from GlobalRouterMap (public schema).
        Frees up the VPN IP for future allocation.
        """
        from django_tenants.utils import schema_context, get_public_schema_name
        from apps.core.models import GlobalRouterMap

        if not router.vpn_ip_address:
            logger.debug(f"Router {router.name} has no VPN IP to unregister")
            return

        logger.info(f"Unregistering router {router.name} (IP: {router.vpn_ip_address}) from GlobalRouterMap")

        with schema_context(get_public_schema_name()):
            deleted_count, _ = GlobalRouterMap.objects.filter(
                nas_ip=router.vpn_ip_address
            ).delete()
            
            if deleted_count > 0:
                logger.info(f"Removed GlobalRouterMap entry for IP {router.vpn_ip_address}")
            else:
                logger.warning(f"No GlobalRouterMap entry found for IP {router.vpn_ip_address}")

    def _generate_cn(self, router) -> str:
        """
        Generate the Common Name for the certificate and CCD file.
        Because the OpenVPN server uses 'username-as-common-name',
        we must use the router's OpenVPN username directly.
        """
        # Ensure the router has an openvpn_username (it should be auto-generated)
        if not router.openvpn_username:
            raise VPNProvisioningError(
                f"Router {router.name} has no openvpn_username. "
                "Make sure Router.save() generates it."
            )
        return router.openvpn_username

    def _generate_client_certificate(
        self, ca: CertificateAuthority, router, common_name: str
    ) -> VPNCertificate:
        """Generate a client certificate using the CertificateService."""
        # Check if there's an existing active cert for this router
        existing = VPNCertificate.objects.filter(
            router=router,
            status='active',
            certificate_type='client',
        ).first()
        
        if existing:
            # Revoke old cert before generating new one
            existing.revoke(reason="Replaced by new provisioning")

        # The CertificateService handles DB creation and returns the Cert object
        cert_record = self.cert_service.generate_client_certificate(
            ca=ca,
            router=router,
            common_name=common_name,
        )

        return cert_record