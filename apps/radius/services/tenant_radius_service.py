"""
Tenant RADIUS Configuration Service (PURE SHARED ARCHITECTURE)

This service is now 100% database-driven.
Zero files or folders are generated per tenant.
Zero Docker commands executed from Python.

NOTE (2026-10): RADIUS is fully shared at the platform level.
Routing lives in core_globalroutermap (public schema), which is written
whenever a router is added via the admin UI. FreeRADIUS reads that map
directly. Nothing is provisioned per tenant.

Historically this service tried to write a RadiusTenantConfig row inside
schema_context('public'), but RadiusTenantConfig lives in apps.radius,
which is a TENANT_APP — so its table only exists inside each tenant
schema, not in public. The query always failed. Because the failure was
in a code path only used by admin actions (not the router-creation hot
path), it was silent and harmless in practice. This module is now a
no-op to remove that dead code.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class TenantRadiusConfigService:
    """RADIUS is fully shared: routing lives in core_globalroutermap (public).
    Nothing to provision per tenant."""

    def configure_tenant_radius(self, schema_name: str, tenant_name: str = None,
                                regenerate: bool = False, **_ignored) -> Dict[str, Any]:
        if not schema_name or schema_name == 'public':
            raise ValueError("Cannot configure RADIUS for public schema")
        logger.debug("RADIUS ready for tenant %s", schema_name)
        return {'schema_name': schema_name, 'status': 'ready'}

    def remove_tenant_radius(self, schema_name: str) -> bool:
        return True


# Singleton instance
tenant_radius_service = TenantRadiusConfigService()