"""
Cloud Portal Views — Cloud Controller Architecture

These views support the Cloud Redirector flow:
1. login-page/{router_id}/ — Serves dynamic login.html (fetched by MikroTik)
2. auto-login/ — MAC-based auto-login check
3. device-auth/ — Smart TV / multi-device authorization
4. return-trip/ — Completes the "Return Trip" back to MikroTik after payment
5. tv/generate-code/ — Generates 5-char code for Smart TV pairing
6. tv/verify-code/ — Verifies TV code from mobile device

All endpoints are PUBLIC (no auth required — used from captive portal).
"""

import logging
import random
import string
from contextlib import contextmanager

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django_tenants.utils import schema_context, get_public_schema_name

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.core.models import Tenant
from apps.network.models.router_models import Router
from apps.billing.models.hotspot_models import HotspotSession

logger = logging.getLogger(__name__)


def _normalize_mac(mac: str) -> str:
    if not mac:
        return ""
    return (mac or "").strip().upper().replace("-", ":")


def _resolve_tenant(tenant_value: str):
    """Safely resolve tenant from public schema"""
    if not tenant_value:
        return None

    with schema_context(get_public_schema_name()):
        return Tenant.objects.filter(
            Q(subdomain=tenant_value) | Q(schema_name=tenant_value),
            is_active=True,
        ).first()


def _tv_code_cache_key(schema_name: str, code: str) -> str:
    """Cache key for TV pairing code"""
    return f"tv_code:{schema_name}:{code}"


def _tv_device_cache_key(schema_name: str, router_id: str, mac: str) -> str:
    """Cache key for TV device to code mapping"""
    return f"tv_device:{schema_name}:{router_id}:{mac}"


def _generate_tv_code(length: int = 5) -> str:
    """Generate a human-friendly TV pairing code (excludes ambiguous characters)"""
    # Exclude ambiguous chars: 0/O, 1/I/L
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return ''.join(random.choice(alphabet) for _ in range(length))


def _ensure_session_radius_credentials(session: HotspotSession) -> bool:
    """Ensure RADIUS credentials exist for an active/paid session.
    
    This is the core reconnect fix. It re-seeds credentials when user 
    returns after router reboot.
    """
    if session.status not in ('active', 'paid'):
        return False

    if session.status == 'paid':
        try:
            session.activate(session.access_code)
        except Exception as exc:
            logger.error("Failed to activate paid session %s: %s", session.session_id, exc, exc_info=True)
            return False

    if not session.access_code:
        logger.warning("Auto-login: session %s is active but has no access_code", session.session_id)
        return False

    try:
        from apps.billing.services.hotspot_radius_service import HotspotRadiusService

        radius_service = HotspotRadiusService()
        ok = radius_service.create_hotspot_credentials(
            username=session.access_code,
            password=session.access_code,
            router=session.router,
            plan=session.plan,
            expires_at=session.expires_at,
            mac_address=session.mac_address or '',
        )
        
        if not ok:
            logger.error("RADIUS reseed returned False for session %s", session.session_id)
            return False
            
        logger.info("Successfully reseeded RADIUS credentials for session %s", session.session_id)
        return True
    except Exception as exc:
        logger.error("Failed to reseed hotspot RADIUS credentials for %s: %s", session.session_id, exc, exc_info=True)
        return False


@contextmanager
def _tenant_ctx(tenant_subdomain: str):
    """Context manager for tenant operations"""
    tenant = _resolve_tenant(tenant_subdomain)
    if not tenant:
        raise ValueError("Invalid tenant")
    with schema_context(tenant.schema_name):
        yield tenant


class AutoLoginRateThrottle(AnonRateThrottle):
    scope = "hotspot_auto_login"


class TVCodeGenerateRateThrottle(AnonRateThrottle):
    scope = "hotspot_tv_code_generate"


class TVCodeVerifyRateThrottle(AnonRateThrottle):
    scope = "hotspot_tv_code_verify"


class HotspotLoginPageView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, router_id):
        tenant_subdomain = request.query_params.get("tenant")
        if not tenant_subdomain:
            return HttpResponse("Tenant parameter required", status=400)

        try:
            with _tenant_ctx(tenant_subdomain):
                try:
                    router = Router.objects.get(id=router_id, is_active=True)
                except Router.DoesNotExist:
                    return HttpResponse(
                        "<html><body><h1>Router not found</h1></body></html>",
                        content_type="text/html",
                        status=404,
                    )

                portal_url = getattr(settings, "CAPTIVE_PORTAL_URL", settings.BASE_URL).rstrip("/")
                html = self._generate_login_html(router, portal_url, tenant_subdomain)
                return HttpResponse(html, content_type="text/html")
        except ValueError:
            return HttpResponse("Invalid tenant", status=400)

    def _generate_login_html(self, router, portal_url: str, tenant_subdomain: str) -> str:
        tenant_param = tenant_subdomain or ""
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="pragma" content="no-cache">
  <title>Connecting...</title>
</head>
<body>
  <script>
  (function() {{
    var mac = '$(mac)', ip = '$(ip)', identity = '$(identity)';
    var loginUrl = '$(link-login-only)', error = '$(error)';
    var ua = navigator.userAgent.toLowerCase();
    var smartTV = /smart-?tv|webos|tizen|vidaa|hbbtv|roku|firetv|apple\\s?tv/i.test(ua) ? '1' : '0';

    var p = 'mac=' + encodeURIComponent(mac) +
            '&ip=' + encodeURIComponent(ip) +
            '&router=' + encodeURIComponent(identity) +
            '&login_url=' + encodeURIComponent(loginUrl) +
            '&error=' + encodeURIComponent(error) +
            '&tenant={tenant_param}' +
            '&smart_tv=' + smartTV;

    var url = '{portal_url}/hotspot/{router.id}?' + p;
    window.location.href = url;
  }})();
  </script>
</body>
</html>"""


class HotspotAutoLoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [AutoLoginRateThrottle]

    def post(self, request):
        tenant_subdomain = request.data.get("tenant") or request.query_params.get("tenant")
        router_id = request.data.get("router_id")
        mac_address = _normalize_mac(request.data.get("mac_address"))

        if not tenant_subdomain:
            return Response({"error": "Tenant is missing."}, status=status.HTTP_400_BAD_REQUEST)
        if not router_id or not mac_address:
            return Response({"error": "Missing router/mac"}, status=status.HTTP_400_BAD_REQUEST)

        # Resolve tenant first
        tenant = _resolve_tenant(tenant_subdomain)
        if not tenant:
            return Response({"error": "Invalid tenant."}, status=status.HTTP_400_BAD_REQUEST)

        # Normalize MAC again to ensure consistency
        mac_address = mac_address.upper().replace("-", ":")

        with schema_context(tenant.schema_name):
            # router lookup by id or name
            try:
                router = Router.objects.get(id=router_id, is_active=True)
            except (Router.DoesNotExist, ValueError):
                try:
                    router = Router.objects.get(name=router_id, is_active=True)
                except Router.DoesNotExist:
                    return Response({"error": "Router not found"}, status=status.HTTP_400_BAD_REQUEST)

            # Find session - handles both paid and active
            active_session = HotspotSession.objects.filter(
                router=router,
                mac_address=mac_address,
            ).filter(
                Q(status='paid') |
                Q(status='active', expires_at__gt=timezone.now())
            ).order_by('-created_at').first()

            if not active_session:
                return Response({"has_session": False})

            # Ensure credentials exist before returning session
            if not _ensure_session_radius_credentials(active_session):
                logger.warning(f"Auto-login: session {active_session.session_id} has no valid credentials")
                return Response({"has_session": False, "reason": "Session credentials unavailable"})

            # Guard against missing expires_at
            remaining_minutes = 0
            if active_session.expires_at:
                remaining_minutes = max(
                    int((active_session.expires_at - timezone.now()).total_seconds() / 60), 0
                )

            return Response({
                "has_session": True,
                "session_id": active_session.session_id,
                "access_code": active_session.access_code,
                "plan_name": active_session.plan.name,
                "expires_at": active_session.expires_at.isoformat() if active_session.expires_at else None,
                "remaining_minutes": remaining_minutes,
                "data_remaining_mb": active_session.data_remaining_mb,
                "speed": f"{active_session.plan.speed_limit_mbps}Mbps",
                "credentials": {
                    "username": active_session.access_code,
                    "password": active_session.access_code,
                },
            })


class HotspotReturnTripView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, session_id):
        tenant_subdomain = request.query_params.get("tenant")
        if not tenant_subdomain:
            return Response({"error": "Tenant parameter required"}, status=400)

        # Resolve tenant first
        tenant = _resolve_tenant(tenant_subdomain)
        if not tenant:
            return Response({"error": "Invalid tenant provided"}, status=400)

        with schema_context(tenant.schema_name):
            try:
                session = HotspotSession.objects.select_related("plan", "router").get(session_id=session_id)
            except HotspotSession.DoesNotExist:
                return Response({"error": "Session not found"}, status=status.HTTP_404_NOT_FOUND)

            if session.status not in ("active", "paid"):
                return Response({"status": session.status, "message": "Session is not ready for authentication."})

            # Ensure credentials exist before returning login details
            if not _ensure_session_radius_credentials(session):
                return Response(
                    {"status": "error", "message": "Session credentials unavailable. Please refresh and try again."},
                    status=status.HTTP_409_CONFLICT,
                )

            login_url = request.query_params.get("login_url") or f"http://{session.router.gateway_ip}/login"
            return Response({
                "status": "ready",
                "session_id": session.session_id,
                "login_url": login_url,
                "username": session.access_code,
                "password": session.access_code,
                "method": "auto_submit",
                "plan": {
                    "name": session.plan.name,
                    "duration_display": session.plan.duration_display,
                    "speed": f"{session.plan.speed_limit_mbps}Mbps",
                },
            })


class GenerateTVCodeView(APIView):
    """
    GET /api/v1/hotspot/tv/generate-code/?tenant=...&router_id=...&mac_address=...
    
    Generates a 5-character code for Smart TV pairing.
    The TV displays this code, user enters it on their phone.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [TVCodeGenerateRateThrottle]

    def get(self, request):
        tenant_subdomain = request.query_params.get('tenant')
        router_id = request.query_params.get('router_id')
        mac_address = _normalize_mac(request.query_params.get('mac_address'))

        # Validation
        if not tenant_subdomain:
            return Response({'error': 'Tenant is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        if not router_id or not mac_address or mac_address == '00:00:00:00:00:00':
            return Response(
                {'error': 'Valid router_id and mac_address are required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # Resolve tenant
        tenant = _resolve_tenant(tenant_subdomain)
        if not tenant:
            return Response({'error': 'Invalid tenant'}, status=status.HTTP_400_BAD_REQUEST)

        with schema_context(tenant.schema_name):
            # Validate router exists in this tenant
            try:
                router = Router.objects.get(id=router_id, is_active=True)
            except (Router.DoesNotExist, ValueError):
                try:
                    router = Router.objects.get(name=router_id, is_active=True)
                except Router.DoesNotExist:
                    return Response(
                        {'error': 'Router not found'}, 
                        status=status.HTTP_404_NOT_FOUND
                    )

            # Reuse active unexpired code for same TV+router
            device_key = _tv_device_cache_key(tenant.schema_name, str(router.id), mac_address)
            existing_code = cache.get(device_key)
            if existing_code:
                return Response({'code': existing_code, 'expires_in': 300})

            # Generate unique 5-char code (best effort with retries)
            code = None
            for _ in range(10):  # Try up to 10 times to avoid collision
                candidate = _generate_tv_code(5)
                code_key = _tv_code_cache_key(tenant.schema_name, candidate)
                if not cache.get(code_key):
                    code = candidate
                    break

            if not code:
                logger.error(f"Failed to generate unique TV code for router {router.id}, mac {mac_address}")
                return Response(
                    {'error': 'Unable to generate code. Please try again.'}, 
                    status=status.HTTP_503_SERVICE_UNAVAILABLE
                )

            # Store pairing data
            payload = {
                'mac_address': mac_address,
                'router_id': str(router.id),
                'tenant': tenant.subdomain,
                'created_at': timezone.now().isoformat(),
            }

            # TTL 5 minutes (good UX + secure)
            cache.set(_tv_code_cache_key(tenant.schema_name, code), payload, timeout=300)
            cache.set(device_key, code, timeout=300)

            logger.info(f"Generated TV code {code} for router {router.id}, mac {mac_address}")
            return Response({'code': code, 'expires_in': 300})


class VerifyTVCodeView(APIView):
    """
    POST /api/v1/hotspot/tv/verify-code/
    body: { "tenant": "...", "code": "B7X9Q" }
    
    Verifies the TV code entered by user on their phone.
    Returns the TV's MAC address and router ID for payment.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [TVCodeVerifyRateThrottle]

    def post(self, request):
        tenant_subdomain = request.data.get('tenant') or request.query_params.get('tenant')
        code = (request.data.get('code') or '').strip().upper()

        # Validation
        if not tenant_subdomain:
            return Response({'error': 'Tenant is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        if len(code) != 5:
            return Response({'error': 'Invalid code format'}, status=status.HTTP_400_BAD_REQUEST)

        # Resolve tenant
        tenant = _resolve_tenant(tenant_subdomain)
        if not tenant:
            return Response({'error': 'Invalid tenant'}, status=status.HTTP_400_BAD_REQUEST)

        # Look up code in cache
        payload = cache.get(_tv_code_cache_key(tenant.schema_name, code))
        if not payload:
            logger.warning(f"Invalid or expired TV code attempt: {code}")
            return Response(
                {'error': 'Invalid or expired TV code. Please check the TV screen.'}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # Do NOT delete yet; keep valid until payment submit or expiry
        logger.info(f"TV code {code} verified for router {payload['router_id']}, mac {payload['mac_address']}")
        return Response({
            'message': 'TV Found',
            'mac_address': payload['mac_address'],
            'router_id': payload['router_id'],
            'code': code,
        })


class HotspotDeviceAuthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        tenant_subdomain = request.data.get("tenant") or request.query_params.get("tenant")
        if not tenant_subdomain:
            return Response({"error": "Tenant parameter required"}, status=400)

        # Resolve tenant first
        tenant = _resolve_tenant(tenant_subdomain)
        if not tenant:
            return Response({"error": "Invalid tenant provided"}, status=400)

        action = request.path.rstrip("/").split("/")[-1]
        
        with schema_context(tenant.schema_name):
            if action == "request":
                return self._request_pairing(request)
            if action == "authorize":
                return self._authorize_device(request)
            return Response({"error": "Invalid action"}, status=status.HTTP_400_BAD_REQUEST)

    def _request_pairing(self, request):
        router_id = request.data.get("router_id")
        mac_address = _normalize_mac(request.data.get("mac_address", ""))
        device_type = request.data.get("device_type", "unknown")

        if not router_id or not mac_address:
            return Response({"error": "router_id and mac_address are required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            Router.objects.get(id=router_id, is_active=True)
        except Router.DoesNotExist:
            return Response({"error": "Router not found"}, status=status.HTTP_404_NOT_FOUND)

        pairing_code = str(random.randint(100000, 999999))
        cache.set(
            f"device_pairing:{pairing_code}",
            {
                "router_id": str(router_id),
                "mac_address": mac_address,
                "device_type": device_type,
                "created_at": timezone.now().isoformat(),
            },
            timeout=300,
        )
        cache.set(f"device_pairing_mac:{router_id}:{mac_address}", pairing_code, timeout=300)

        return Response({
            "pairing_code": pairing_code,
            "expires_in": 300,
            "message": f"Enter code {pairing_code} on your phone to authorize this device.",
        })

    def _authorize_device(self, request):
        pairing_code = request.data.get("pairing_code", "")
        session_id = request.data.get("session_id", "")

        if not pairing_code or not session_id:
            return Response({"error": "pairing_code and session_id are required"}, status=status.HTTP_400_BAD_REQUEST)

        pairing_data = cache.get(f"device_pairing:{pairing_code}")
        if not pairing_data:
            return Response({"error": "Invalid or expired pairing code"}, status=status.HTTP_404_NOT_FOUND)

        try:
            session = HotspotSession.objects.get(
                session_id=session_id, status="active", expires_at__gt=timezone.now()
            )
        except HotspotSession.DoesNotExist:
            return Response({"error": "Invalid or expired session"}, status=status.HTTP_400_BAD_REQUEST)

        if str(session.router_id) != str(pairing_data["router_id"]):
            return Response({"error": "Session and device must be on the same router"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from apps.billing.services.hotspot_radius_service import HotspotRadiusService
            HotspotRadiusService().create_mac_auth_entry(
                mac_address=pairing_data["mac_address"],
                router=session.router,
                plan=session.plan,
                expires_at=session.expires_at,
            )
        except Exception as e:
            logger.error(f"Failed to create device RADIUS entry: {e}")
            return Response({"error": "Failed to authorize device. Please try again."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        cache.delete(f"device_pairing:{pairing_code}")
        cache.delete(f"device_pairing_mac:{pairing_data['router_id']}:{pairing_data['mac_address']}")

        return Response({
            "status": "authorized",
            "device_mac": pairing_data["mac_address"],
            "plan_name": session.plan.name,
            "expires_at": session.expires_at.isoformat(),
            "message": "Device authorized! It should connect within 30 seconds.",
        })


class HotspotDeviceAuthStatusView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        tenant_subdomain = request.query_params.get("tenant")
        if not tenant_subdomain:
            return Response({"error": "Tenant parameter required"}, status=400)

        router_id = request.query_params.get("router_id")
        mac_address = _normalize_mac(request.query_params.get("mac", ""))

        if not router_id or not mac_address:
            return Response({"error": "router_id and mac are required"}, status=status.HTTP_400_BAD_REQUEST)

        # Resolve tenant first
        tenant = _resolve_tenant(tenant_subdomain)
        if not tenant:
            return Response({"error": "Invalid tenant provided"}, status=400)

        with schema_context(tenant.schema_name):
            pairing_code = cache.get(f"device_pairing_mac:{router_id}:{mac_address}")
            if pairing_code:
                return Response({
                    "status": "waiting",
                    "pairing_code": pairing_code,
                    "message": "Waiting for authorization...",
                })

            active_session = HotspotSession.objects.filter(
                router_id=router_id,
                mac_address=mac_address,
                status="active",
                expires_at__gt=timezone.now(),
            ).first()

            if active_session:
                return Response({
                    "status": "authorized",
                    "access_code": active_session.access_code,
                    "message": "Device authorized! Connecting...",
                })

            return Response({"status": "not_found", "message": "No pairing request found."})