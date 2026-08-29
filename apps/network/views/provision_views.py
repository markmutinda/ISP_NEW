# apps/network/views/provision_views.py
"""
Public Provisioning Endpoints for MikroTik /tool fetch

These endpoints are UNAUTHENTICATED — they are called by MikroTik
routers during the zero-touch provisioning process. Security is
provided by the auth_key + provision_slug combination (and optionally
IP-based rate limiting in production).

Endpoints:
──────────
Stage 1 (Base Script):
    GET /api/v1/network/provision/{auth_key}/{slug}/script.rsc
        → Returns the base script (version detection + Stage 2 fetch)

Stage 2 (Config):
    GET /api/v1/network/provision/{auth_key}/config?version=7&router=1&subdomain=xyz
        → Returns the version-specific full configuration script

Certs:
    GET /api/v1/network/provision/{auth_key}/certs/ca.crt
    GET /api/v1/network/provision/{auth_key}/certs/ssl.crt
    GET /api/v1/network/provision/{auth_key}/certs/ssl.key

Hotspot HTML:
    GET /api/v1/network/provision/{auth_key}/hotspot/login.html
    GET /api/v1/network/provision/{auth_key}/hotspot/rlogin.html
    GET /api/v1/network/provision/{auth_key}/hotspot/alogin.html
    GET /api/v1/network/provision/{auth_key}/hotspot/redirect.html
    GET /api/v1/network/provision/{auth_key}/hotspot/error.html
    GET /api/v1/network/provision/{auth_key}/hotspot/status.html
"""

import logging

from django.http import HttpResponse, Http404
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.throttling import AnonRateThrottle

from apps.network.models.router_models import Router
from apps.network.services.mikrotik_script_generator import MikrotikScriptGenerator

logger = logging.getLogger(__name__)


# ─── Rate Limiting ────────────────────────────────────────────
class ProvisionRateThrottle(AnonRateThrottle):
    """Limit provisioning requests to prevent abuse."""
    rate = '30/hour'


# ─── Helper ───────────────────────────────────────────────────
def _get_router_by_auth_key(auth_key: str) -> Router:
    """
    Lookup router by auth_key across all ISP tenants. Raises Http404 if not found.
    
    FIX: Properly resets schema between tenant attempts to avoid
    querying the wrong schema on subsequent iterations.
    """
    from django.db import connection
    from apps.core.models import Tenant
    from django_tenants.utils import schema_context

    # Fast path: try RouterTenantIndex
    try:
        from apps.core.models import RouterTenantIndex
        with schema_context('public'):
            index_row = RouterTenantIndex.objects.select_related('tenant').filter(
                router_auth_key=auth_key,
                is_active=True,
            ).first()

        if index_row:
            with schema_context(index_row.tenant.schema_name):
                router = Router.objects.filter(auth_key=auth_key, is_active=True).first()
            if router:
                connection.set_tenant(index_row.tenant)
                logger.debug(f"[PROVISION] Router found via index: {router.name}")
                return router
    except Exception as e:
        logger.warning(f"[PROVISION] Index lookup failed: {e}")

    # Fallback: scan all tenants
    try:
        with schema_context('public'):
            tenants = list(Tenant.objects.filter(is_active=True).exclude(schema_name='public'))
    except Exception as e:
        logger.error(f"[PROVISION] Cannot list tenants: {e}")
        raise Http404("Router not found")

    for tenant in tenants:
        try:
            with schema_context(tenant.schema_name):
                router = Router.objects.filter(auth_key=auth_key, is_active=True).first()
                if router:
                    connection.set_tenant(tenant)
                    logger.debug(f"[PROVISION] Router found via scan: {router.name} (tenant={tenant.schema_name})")
                    return router
        except Exception as e:
            logger.warning(f"[PROVISION] Error checking tenant {tenant.schema_name}: {e}")
            continue

    connection.set_schema_to_public()
    raise Http404("Router not found")


def _plain_text_response(content: str, filename: str = None) -> HttpResponse:
    """Return a plain-text HTTP response (for MikroTik /tool fetch)."""
    response = HttpResponse(content, content_type='text/plain; charset=utf-8')
    if filename:
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
    # No caching — scripts are dynamic
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


def _html_response(content: str) -> HttpResponse:
    """Return an HTML HTTP response."""
    response = HttpResponse(content, content_type='text/html; charset=utf-8')
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


def _cert_response(content: str, filename: str) -> HttpResponse:
    """Return a PEM certificate as downloadable file."""
    response = HttpResponse(
        content,
        content_type='application/x-pem-file'
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STAGE 1 — Base Script Download (The "Magic Link" endpoint)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class ProvisionBaseScriptView(APIView):
    """
    GET /api/v1/network/provision/{auth_key}/{slug}/script.rsc

    Returns the Stage 1 base script that detects RouterOS version
    and triggers the Stage 2 config download.

    The {slug} parameter provides secondary validation:
      - Must match the router's provision_slug
      - Prevents brute-force enumeration of auth_keys
    """
    permission_classes = [AllowAny]
    throttle_classes = [ProvisionRateThrottle]
    authentication_classes = []  # No auth needed

    def get(self, request, auth_key, slug):
        try:
            router = _get_router_by_auth_key(auth_key)
        except Http404:
            logger.error(f"[PROVISION BASE] Router not found for auth_key={auth_key}")
            raise

        # Validate slug
        if router.provision_slug and router.provision_slug != slug:
            logger.warning(
                f"[PROVISION BASE] Slug mismatch for router '{router.name}': "
                f"expected={router.provision_slug}, got={slug}"
            )
            raise Http404("Invalid provisioning link")

        # Generate Stage 1 script
        gen = MikrotikScriptGenerator(router, request=request)
        script = gen.generate_base_script()

        # Log the provisioning attempt
        logger.info(
            f"Provision Stage 1: Router '{router.name}' (id={router.id}) "
            f"downloaded base script from {request.META.get('REMOTE_ADDR', '?')}"
        )

        return _plain_text_response(script, filename='netily.rsc')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STAGE 2 — Version-Specific Config Download
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class ProvisionConfigView(APIView):
    """
    GET /api/v1/network/provision/{auth_key}/{slug}/config?version=7&router=1&subdomain=xyz

    Returns the full RouterOS configuration script, tailored to the
    detected version (6 or 7). Called by the Stage 1 base script.

    Path Parameters:
        auth_key — Router authentication key
        slug     — Provision slug (for secondary validation)

    Query Parameters:
        version  — RouterOS major version ("6" or "7")
        router   — Router ID (for validation)
        subdomain — Tenant subdomain (for cross-check)
    """
    permission_classes = [AllowAny]
    throttle_classes = [ProvisionRateThrottle]
    authentication_classes = []

    def get(self, request, auth_key, slug):
        try:
            router = _get_router_by_auth_key(auth_key)
        except Http404:
            logger.error(f"[PROVISION CONFIG] Router not found for auth_key={auth_key}")
            raise

        # Validate slug (same security check as Stage 1)
        if router.provision_slug and router.provision_slug != slug:
            logger.warning(
                f"[PROVISION CONFIG] Slug mismatch for router '{router.name}': "
                f"expected={router.provision_slug}, got={slug}"
            )
            raise Http404("Invalid provisioning link")

        # Extract query params
        version = request.query_params.get('version', '7')
        router_id = request.query_params.get('router', None)

        # Optional cross-validation (if Stage 1 sent the router ID)
        if router_id and str(router.id) != str(router_id):
            logger.warning(
                f"[PROVISION CONFIG] Router ID mismatch: auth_key={auth_key}, "
                f"expected={router.id}, got={router_id}"
            )
            raise Http404("Router mismatch")

        # Generate the version-specific config
        try:
            gen = MikrotikScriptGenerator(router)
            config = gen.generate_config_script(version)
        except Exception as e:
            logger.error(
                f"[PROVISION CONFIG] Script generation failed for router '{router.name}': {e}",
                exc_info=True
            )
            # Return a plain text error script so MikroTik can download and show the error
            error_script = f"""# Netily Provisioning Error
# Router: {router.name}
# Router ID: {router.id}
# Error: {str(e)}
# Please check backend logs.

:put "========================================="
:put "PROVISIONING ERROR"
:put "========================================="
:put "Router: {router.name}"
:put "Error: {str(e)}"
:put "========================================="
:put "Please contact support with this error."
:put "========================================="
"""
            return _plain_text_response(error_script, filename='error.rsc')

        # FIX: Update router provisioning state WITHOUT triggering signals
        # Using update() bypasses the save() signals that can hang for 10+ seconds
        # causing MikroTik /tool fetch to time out with "timeout waiting data"
        Router.objects.filter(pk=router.pk).update(
            routeros_version=version,
            last_provisioned_at=timezone.now()
        )

        logger.info(
            f"[PROVISION CONFIG] Router '{router.name}' (id={router.id}) "
            f"downloaded v{version} config from {request.META.get('REMOTE_ADDR', '?')}"
        )

        return _plain_text_response(config, filename='netily_conf.rsc')


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CERTIFICATE DOWNLOADS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class ProvisionCertView(APIView):
    """
    GET /api/v1/network/provision/{auth_key}/certs/{cert_type}

    Downloads certificate files for the router.
    
    cert_type options:
        ca.crt   — OpenVPN CA certificate (for TLS verification)
        ssl.crt  — Hotspot SSL certificate
        ssl.key  — Hotspot SSL private key
    """
    permission_classes = [AllowAny]
    throttle_classes = [ProvisionRateThrottle]
    authentication_classes = []

    CERT_MAP = {
        'ca.crt': ('ca_certificate', 'ca.crt'),
        'ssl.crt': ('ssl_certificate', 'ssl.crt'),
        'ssl.key': ('ssl_private_key', 'ssl.key'),
    }

    def get(self, request, auth_key, cert_type):
        try:
            router = _get_router_by_auth_key(auth_key)
        except Http404:
            logger.error(f"[PROVISION CERT] Router not found for auth_key={auth_key}")
            raise

        if cert_type not in self.CERT_MAP:
            logger.warning(f"[PROVISION CERT] Unknown cert type: {cert_type} for router '{router.name}'")
            raise Http404(f"Unknown cert type: {cert_type}")

        field_name, filename = self.CERT_MAP[cert_type]
        content = getattr(router, field_name, None)

        if not content:
            logger.warning(
                f"[PROVISION CERT] Certificate '{cert_type}' not configured for router '{router.name}'"
            )
            raise Http404(f"Certificate '{cert_type}' not configured for this router")

        logger.info(
            f"Provision cert: Router '{router.name}' downloaded {cert_type} "
            f"from {request.META.get('REMOTE_ADDR', '?')}"
        )

        return _cert_response(content, filename)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HOTSPOT HTML DOWNLOADS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class ProvisionHotspotHTMLView(APIView):
    """
    GET /api/v1/network/provision/{auth_key}/hotspot/{page}

    Downloads hotspot HTML pages.

    page options:
        login.html    — Cloud portal redirector (MikroTik captive portal intercept)
        rlogin.html   — Intercepted/redirected pre-auth requests (lightweight loading)
        alogin.html   — Post-auto-login success (MAC-cookie / trial)
        redirect.html — RouterOS-mandated 302 redirect shim
        error.html    — Error page with retry logic
        status.html   — Post-authentication status page
    """
    permission_classes = [AllowAny]
    throttle_classes = [ProvisionRateThrottle]
    authentication_classes = []

    def get(self, request, auth_key, page):
        try:
            router = _get_router_by_auth_key(auth_key)
        except Http404:
            logger.error(f"[PROVISION HTML] Router not found for auth_key={auth_key}")
            raise

        gen = MikrotikScriptGenerator(router)

        # 🔥 FIX: Dispatch all 6 pages
        generators = {
            'login.html': gen.generate_login_html,
            'rlogin.html': gen.generate_rlogin_html,
            'alogin.html': gen.generate_alogin_html,
            'redirect.html': gen.generate_redirect_html,
            'error.html': gen.generate_error_html,
            'status.html': gen.generate_status_html,
        }

        handler = generators.get(page)
        if not handler:
            logger.warning(f"[PROVISION HTML] Unknown hotspot page: {page} for router '{router.name}'")
            raise Http404(f"Unknown hotspot page: {page}")

        html = handler()

        # 🔥 Stamp DB the moment the router actually fetches login.html
        if page == 'login.html':
            from apps.network.services.mikrotik_script_generator import LOGIN_HTML_VERSION
            Router.objects.filter(pk=router.pk).update(last_login_html_version=LOGIN_HTML_VERSION)
            logger.info(f"[PROVISION HTML] Stamped login.html version {LOGIN_HTML_VERSION} for router {router.id}")

        logger.info(
            f"Provision HTML: Router '{router.name}' downloaded {page} "
            f"from {request.META.get('REMOTE_ADDR', '?')}"
        )

        return _html_response(html)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LEGACY COMPAT — Old single-script download
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class LegacyScriptDownloadView(APIView):
    """
    GET /api/v1/network/routers/config/?auth_key=RTR_xxx

    Legacy endpoint: Downloads the full v7 script in one shot.
    Kept for backward compatibility with already-deployed routers.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        auth_key = request.query_params.get('auth_key')
        if not auth_key:
            logger.error("[PROVISION LEGACY] Missing auth_key parameter")
            raise Http404("Missing auth_key")

        try:
            router = _get_router_by_auth_key(auth_key)
        except Http404:
            logger.error(f"[PROVISION LEGACY] Router not found for auth_key={auth_key}")
            raise

        gen = MikrotikScriptGenerator(router)
        script = gen.generate_config_script("7")

        logger.info(
            f"Provision Legacy: Router '{router.name}' (id={router.id}) "
            f"downloaded full script from {request.META.get('REMOTE_ADDR', '?')}"
        )

        return _plain_text_response(script, filename='netily_setup.rsc')