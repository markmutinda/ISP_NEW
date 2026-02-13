"""
VPN Provisioning Service — Cloud Controller Auto-Provisioning

Handles the full lifecycle when a new Router is created:
1. Assigns next available static VPN IP from the 10.8.0.0/24 pool
2. Generates a client certificate via CertificateService
3. Writes a CCD file mapping the certificate CN → static IP
4. Stores PEM content on the Router model for .rsc script injection
"""

import logging
from typing import Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.vpn.models import CertificateAuthority, VPNCertificate, VPNServer
from apps.vpn.services.certificate_service import CertificateService
from apps.vpn.services.ccd_manager import CCDManager

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
        2. Assign a unique VPN IP
        3. Generate a client certificate
        4. Write the CCD file
        5. Store everything on the Router record
        """
        from apps.network.models.router_models import Router  # late import to avoid circular

        logger.info(f"Starting VPN provisioning for router: {router.name} (id={router.id})")

        try:
            with transaction.atomic():
                # 1. Ensure CA exists
                ca = self._ensure_ca()

                # 2. Assign VPN IP
                vpn_ip = self._assign_vpn_ip(router)
                logger.info(f"Assigned VPN IP {vpn_ip} to router {router.name}")

                # 3. Generate client certificate
                common_name = self._generate_cn(router)
                cert_record = self._generate_client_certificate(ca, router, common_name)
                logger.info(f"Generated certificate CN={common_name} for router {router.name}")

                # 4. Write CCD file
                self.ccd_manager.create_ccd_file(common_name, vpn_ip)
                logger.info(f"Wrote CCD file for CN={common_name} → {vpn_ip}")

                # 5. Update Router record
                router.vpn_ip_address = vpn_ip
                router.vpn_certificate = cert_record
                router.ca_certificate = ca.ca_certificate
                router.client_certificate = cert_record.certificate
                router.client_key = cert_record.private_key
                router.vpn_provisioned = True
                router.vpn_provisioned_at = timezone.now()
                # Also set the management ip_address for backward compat
                router.ip_address = vpn_ip
                router.save(update_fields=[
                    'vpn_ip_address', 'vpn_certificate', 'ca_certificate',
                    'client_certificate', 'client_key', 'vpn_provisioned',
                    'vpn_provisioned_at', 'ip_address', 'updated_at',
                ])

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
        - Clears Router VPN fields
        """
        logger.info(f"Deprovisioning VPN for router: {router.name}")

        # Revoke certificate
        if router.vpn_certificate:
            router.vpn_certificate.revoke(reason=f"Router {router.name} deprovisioned")

        # Remove CCD
        common_name = self._generate_cn(router)
        self.ccd_manager.remove_ccd_file(common_name)

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
        Finds the next available IP in the VPN range.
        Skips .0 (network), .1 (server), and .255 (broadcast).
        """
        from apps.network.models.router_models import Router

        range_start = getattr(settings, 'VPN_IP_RANGE_START', 10)
        range_end = getattr(settings, 'VPN_IP_RANGE_END', 250)

        # If router already has a VPN IP assigned, reuse it
        if router.vpn_ip_address:
            return router.vpn_ip_address

        # Get all assigned VPN IPs
        assigned_ips = set(
            Router.objects.exclude(vpn_ip_address__isnull=True)
            .values_list('vpn_ip_address', flat=True)
        )

        # Find the next available
        base = '10.8.0'  # From VPN_NETWORK_CIDR
        for i in range(range_start, range_end + 1):
            candidate = f"{base}.{i}"
            if candidate not in assigned_ips:
                return candidate

        raise VPNProvisioningError(
            f"No available VPN IPs in range {base}.{range_start}-{base}.{range_end}. "
            f"{len(assigned_ips)} IPs already assigned."
        )

    def _generate_cn(self, router) -> str:
        """Generate a unique Common Name for the certificate."""
        # Format: netily-{router_id}-{sanitized_name}
        safe_name = router.name.lower().replace(' ', '-').replace('_', '-')[:20]
        return f"netily-router-{router.id}-{safe_name}"

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