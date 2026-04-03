"""
Cloud Portal Views — Cloud Controller Architecture

These views support the Cloud Redirector flow:
1. login-page/{router_id}/ — Serves dynamic login.html (fetched by MikroTik)
2. auto-login/ — MAC-based auto-login check
3. device-auth/ — Smart TV / multi-device authorization
4. return-trip/ — Completes the "Return Trip" back to MikroTik after payment

All endpoints are PUBLIC (no auth required — used from captive portal).
"""

import logging
import random
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


def _get_tenant(tenant_subdomain: str):
    if not tenant_subdomain:
        return None
    with schema_context(get_public_schema_name()):
        try:
            return Tenant.objects.get(Q(subdomain=tenant_subdomain) | Q(schema_name=tenant_subdomain))
        except Tenant.DoesNotExist:
            logger.warning(f"Tenant not found: {tenant_subdomain}")
            return None


@contextmanager
def _tenant_ctx(tenant_subdomain: str):
    tenant = _get_tenant(tenant_subdomain)
    if not tenant:
        raise ValueError("Invalid tenant")
    with schema_context(tenant.schema_name):
        yield tenant


class AutoLoginRateThrottle(AnonRateThrottle):
    scope = "hotspot_auto_login"


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

        try:
            with _tenant_ctx(tenant_subdomain):
                # router lookup by id or name
                try:
                    router = Router.objects.get(id=router_id, is_active=True)
                except (Router.DoesNotExist, ValueError):
                    try:
                        router = Router.objects.get(name=router_id, is_active=True)
                    except Router.DoesNotExist:
                        return Response({"error": "Router not found"}, status=status.HTTP_400_BAD_REQUEST)

                session = (
                    HotspotSession.objects.filter(
                        router=router,
                        mac_address=mac_address,
                        status__in=["active", "paid"],
                        expires_at__gt=timezone.now(),
                    )
                    .order_by("-activated_at")
                    .first()
                )

                if not session:
                    return Response({"has_session": False})

                if not session.access_code:
                    logger.warning(f"Session {session.session_id} has no access_code")
                    return Response({"has_session": False})

                # If paid but not active, activate via model method (keeps logic centralized)
                if session.status == "paid":
                    try:
                        session.activate(session.access_code)
                    except Exception as e:
                        logger.error(f"Failed to activate paid session {session.session_id}: {e}")
                        return Response({"has_session": False})

                # Self-heal RADIUS credentials after router reboot
                try:
                    from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                    HotspotRadiusService().create_hotspot_credentials(
                        username=session.access_code,
                        password=session.access_code,
                        router=session.router,
                        plan=session.plan,
                        expires_at=session.expires_at,
                        mac_address=mac_address,
                    )
                except Exception as e:
                    logger.warning(f"RADIUS re-seed failed for {session.session_id}: {e}")

                remaining_minutes = max(
                    int((session.expires_at - timezone.now()).total_seconds() / 60), 0
                )

                return Response({
                    "has_session": True,
                    "session_id": session.session_id,
                    "access_code": session.access_code,
                    "plan_name": session.plan.name,
                    "expires_at": session.expires_at.isoformat(),
                    "remaining_minutes": remaining_minutes,
                    "data_remaining_mb": session.data_remaining_mb,
                    "speed": f"{session.plan.speed_limit_mbps}Mbps",
                    "credentials": {
                        "username": session.access_code,
                        "password": session.access_code,
                    },
                })
        except ValueError:
            return Response({"error": "Invalid tenant."}, status=status.HTTP_400_BAD_REQUEST)


class HotspotReturnTripView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, session_id):
        tenant_subdomain = request.query_params.get("tenant")
        if not tenant_subdomain:
            return Response({"error": "Tenant parameter required"}, status=400)

        try:
            with _tenant_ctx(tenant_subdomain):
                try:
                    session = HotspotSession.objects.select_related("plan", "router").get(session_id=session_id)
                except HotspotSession.DoesNotExist:
                    return Response({"error": "Session not found"}, status=status.HTTP_404_NOT_FOUND)

                if session.status not in ("active", "paid"):
                    return Response({"status": session.status, "message": "Session is not ready for authentication."})

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
        except ValueError:
            return Response({"error": "Invalid tenant provided"}, status=400)


class HotspotDeviceAuthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        tenant_subdomain = request.data.get("tenant") or request.query_params.get("tenant")
        if not tenant_subdomain:
            return Response({"error": "Tenant parameter required"}, status=400)

        action = request.path.rstrip("/").split("/")[-1]
        try:
            with _tenant_ctx(tenant_subdomain):
                if action == "request":
                    return self._request_pairing(request)
                if action == "authorize":
                    return self._authorize_device(request)
                return Response({"error": "Invalid action"}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError:
            return Response({"error": "Invalid tenant provided"}, status=400)

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

        try:
            with _tenant_ctx(tenant_subdomain):
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
        except ValueError:
            return Response({"error": "Invalid tenant provided"}, status=400)