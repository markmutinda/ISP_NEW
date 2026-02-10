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

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.network.models.router_models import Router
from apps.billing.models.hotspot_models import HotspotSession

logger = logging.getLogger(__name__)


class HotspotLoginPageView(APIView):
    """
    Serves the dynamic login.html for MikroTik's /tool fetch.
    
    The MikroTik script generator includes a fallback: if it cannot
    write login.html directly, it fetches from this endpoint.
    
    GET /api/v1/hotspot/login-page/{router_id}/
    
    Returns: text/html (the Cloud Redirector page)
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []
    
    def get(self, request, router_id):
        try:
            router = Router.objects.get(id=router_id, is_active=True)
        except Router.DoesNotExist:
            return HttpResponse(
                '<html><body><h1>Router not found</h1></body></html>',
                content_type='text/html',
                status=404
            )
        
        portal_url = getattr(
            settings, 'CAPTIVE_PORTAL_URL', settings.BASE_URL
        ).rstrip('/')
        
        # Generate the login.html with portal URL baked in
        html = self._generate_login_html(router, portal_url)
        
        return HttpResponse(html, content_type='text/html')
    
    def _generate_login_html(self, router, portal_url: str) -> str:
        """
        Generates the Cloud Redirector HTML with the portal URL embedded.
        MikroTik will replace $(variables) at serve time.
        """
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="pragma" content="no-cache">
    <title>Connecting...</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; color: #333;
        }}
        .card {{
            background: rgba(255,255,255,0.95); border-radius: 16px;
            padding: 40px 32px; text-align: center; max-width: 380px;
            width: 90%; box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        }}
        .spinner {{
            width: 48px; height: 48px; border: 4px solid #e0e0e0;
            border-top: 4px solid #667eea; border-radius: 50%;
            animation: spin 1s linear infinite; margin: 0 auto 24px;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        h2 {{ font-size: 20px; margin-bottom: 8px; }}
        p {{ font-size: 14px; color: #666; margin-bottom: 16px; }}
        .link {{ color: #667eea; text-decoration: none; font-weight: 500; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="spinner"></div>
        <h2>Connecting to WiFi...</h2>
        <p>Redirecting to payment portal...</p>
        <p><a id="lnk" href="#" class="link">Click here if not redirected</a></p>
    </div>
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
                '&smart_tv=' + smartTV;
        
        var url = '{portal_url}/hotspot/{router.id}?' + p;
        document.getElementById('lnk').href = url;
        setTimeout(function() {{ window.location.href = url; }}, 500);
    }})();
    </script>
</body>
</html>"""


class HotspotAutoLoginView(APIView):
    """
    Check if a MAC address has an active session (for auto-login).
    
    When a returning user connects, the portal checks this endpoint.
    If they have a valid (non-expired) session, we can skip payment.
    
    POST /api/v1/hotspot/auto-login/
    {
        "router_id": 5,
        "mac_address": "AA:BB:CC:DD:EE:FF"
    }
    
    Returns:
      - If active session exists: { "has_session": true, "login_url": "...", "credentials": {...} }
      - If no session: { "has_session": false }
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []
    
    def post(self, request):
        router_id = request.data.get('router_id')
        mac_address = request.data.get('mac_address', '').upper().replace('-', ':')
        
        if not router_id or not mac_address:
            return Response(
                {'error': 'router_id and mac_address are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            router = Router.objects.get(id=router_id, is_active=True)
        except Router.DoesNotExist:
            return Response(
                {'error': 'Router not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Find active session for this MAC on this router
        active_session = HotspotSession.objects.filter(
            router=router,
            mac_address=mac_address,
            status='active',
            expires_at__gt=timezone.now()
        ).order_by('-activated_at').first()
        
        if active_session:
            remaining_minutes = int(
                (active_session.expires_at - timezone.now()).total_seconds() / 60
            )
            
            return Response({
                'has_session': True,
                'session_id': active_session.session_id,
                'access_code': active_session.access_code,
                'plan_name': active_session.plan.name,
                'expires_at': active_session.expires_at.isoformat(),
                'remaining_minutes': remaining_minutes,
                'data_remaining_mb': active_session.data_remaining_mb,
                'speed': f"{active_session.plan.speed_limit_mbps}Mbps",
                'credentials': {
                    'username': active_session.access_code,
                    'password': active_session.access_code,
                },
            })
        
        return Response({'has_session': False})


class HotspotReturnTripView(APIView):
    """
    The "Return Trip" — after payment succeeds on the Next.js portal,
    the user must be sent back to the MikroTik login URL to actually
    authenticate and get internet access.
    
    This endpoint provides the RADIUS credentials and MikroTik login URL
    that the frontend uses to complete the authentication loop.
    
    GET /api/v1/hotspot/return-trip/{session_id}/
    
    Returns:
    {
        "status": "ready",
        "login_url": "http://10.0.0.1/login?...",  // MikroTik login URL
        "username": "HS-XXXXXX",
        "password": "HS-XXXXXX",
        "method": "auto_submit"  // or "redirect"
    }
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []
    
    def get(self, request, session_id):
        try:
            session = HotspotSession.objects.select_related('plan', 'router').get(
                session_id=session_id
            )
        except HotspotSession.DoesNotExist:
            return Response(
                {'error': 'Session not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        if session.status not in ('active', 'paid'):
            return Response({
                'status': session.status,
                'message': 'Session is not ready for authentication.',
            })
        
        # The login_url is the MikroTik $(link-login-only) that was captured
        # when the user was first redirected. It's stored on the session
        # or passed back via the frontend query params.
        login_url = request.query_params.get('login_url', '')
        
        if not login_url:
            # Try to construct it from the router's gateway
            login_url = f"http://{session.router.gateway_ip}/login"
        
        return Response({
            'status': 'ready',
            'session_id': session.session_id,
            'login_url': login_url,
            'username': session.access_code,
            'password': session.access_code,
            'method': 'auto_submit',
            'plan': {
                'name': session.plan.name,
                'duration_display': session.plan.duration_display,
                'speed': f"{session.plan.speed_limit_mbps}Mbps",
            },
        })


class HotspotDeviceAuthView(APIView):
    """
    Device Authorization for Smart TVs and limited browsers.
    
    Smart TVs can't do M-Pesa STK push. Instead:
    1. Smart TV gets a 6-digit pairing code
    2. User enters code on their phone (which has internet)
    3. Phone authorizes the TV's MAC address
    4. TV gets internet access via the same session
    
    POST /api/v1/hotspot/device-auth/request/
    {
        "router_id": 5,
        "mac_address": "AA:BB:CC:DD:EE:FF",
        "device_type": "smart_tv"
    }
    → Returns: { "pairing_code": "482916", "expires_in": 300 }
    
    POST /api/v1/hotspot/device-auth/authorize/
    {
        "pairing_code": "482916",
        "session_id": "HS-XXXXXX"  // Active session from the phone
    }
    → Returns: { "status": "authorized" }
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []
    
    def post(self, request):
        action = request.path.rstrip('/').split('/')[-1]
        
        if action == 'request':
            return self._request_pairing(request)
        elif action == 'authorize':
            return self._authorize_device(request)
        
        return Response(
            {'error': 'Invalid action'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    def _request_pairing(self, request):
        """Generate a pairing code for a Smart TV."""
        from django.core.cache import cache
        import random
        
        router_id = request.data.get('router_id')
        mac_address = request.data.get('mac_address', '').upper().replace('-', ':')
        device_type = request.data.get('device_type', 'unknown')
        
        if not router_id or not mac_address:
            return Response(
                {'error': 'router_id and mac_address are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            router = Router.objects.get(id=router_id, is_active=True)
        except Router.DoesNotExist:
            return Response(
                {'error': 'Router not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Generate 6-digit pairing code
        pairing_code = str(random.randint(100000, 999999))
        
        # Store in cache with 5-minute expiry
        cache_key = f'device_pairing:{pairing_code}'
        cache.set(cache_key, {
            'router_id': router_id,
            'mac_address': mac_address,
            'device_type': device_type,
            'created_at': timezone.now().isoformat(),
        }, timeout=300)
        
        # Also store reverse mapping (MAC → code) for status checks
        mac_cache_key = f'device_pairing_mac:{router_id}:{mac_address}'
        cache.set(mac_cache_key, pairing_code, timeout=300)
        
        return Response({
            'pairing_code': pairing_code,
            'expires_in': 300,
            'message': f'Enter code {pairing_code} on your phone to authorize this device.',
        })
    
    def _authorize_device(self, request):
        """Authorize a device using a pairing code + active session."""
        from django.core.cache import cache
        
        pairing_code = request.data.get('pairing_code', '')
        session_id = request.data.get('session_id', '')
        
        if not pairing_code or not session_id:
            return Response(
                {'error': 'pairing_code and session_id are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Look up pairing request
        cache_key = f'device_pairing:{pairing_code}'
        pairing_data = cache.get(cache_key)
        
        if not pairing_data:
            return Response(
                {'error': 'Invalid or expired pairing code'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Look up the authorizing session
        try:
            session = HotspotSession.objects.get(
                session_id=session_id,
                status='active',
                expires_at__gt=timezone.now()
            )
        except HotspotSession.DoesNotExist:
            return Response(
                {'error': 'Invalid or expired session'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Verify same router
        if str(session.router_id) != str(pairing_data['router_id']):
            return Response(
                {'error': 'Session and device must be on the same router'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        device_mac = pairing_data['mac_address']
        
        # Create RADIUS credentials for the device MAC
        try:
            from apps.billing.services.hotspot_radius_service import HotspotRadiusService
            
            radius_service = HotspotRadiusService()
            radius_service.create_mac_auth_entry(
                mac_address=device_mac,
                router=session.router,
                plan=session.plan,
                expires_at=session.expires_at,
            )
            
            logger.info(
                f"Device authorized: MAC={device_mac} via session={session_id} "
                f"on router={session.router.name}"
            )
        except Exception as e:
            logger.error(f"Failed to create device RADIUS entry: {e}")
            return Response(
                {'error': 'Failed to authorize device. Please try again.'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        # Clean up pairing code
        cache.delete(cache_key)
        mac_cache_key = f'device_pairing_mac:{pairing_data["router_id"]}:{device_mac}'
        cache.delete(mac_cache_key)
        
        return Response({
            'status': 'authorized',
            'device_mac': device_mac,
            'plan_name': session.plan.name,
            'expires_at': session.expires_at.isoformat(),
            'message': 'Device authorized! It should connect within 30 seconds.',
        })


class HotspotDeviceAuthStatusView(APIView):
    """
    Check if a device's pairing request has been authorized.
    Smart TVs poll this endpoint after showing the pairing code.
    
    GET /api/v1/hotspot/device-auth/status/?router_id=5&mac=AA:BB:CC:DD:EE:FF
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []
    
    def get(self, request):
        from django.core.cache import cache
        
        router_id = request.query_params.get('router_id')
        mac_address = request.query_params.get('mac', '').upper().replace('-', ':')
        
        if not router_id or not mac_address:
            return Response(
                {'error': 'router_id and mac are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check if pairing code still exists (not yet authorized)
        mac_cache_key = f'device_pairing_mac:{router_id}:{mac_address}'
        pairing_code = cache.get(mac_cache_key)
        
        if pairing_code:
            return Response({
                'status': 'waiting',
                'pairing_code': pairing_code,
                'message': 'Waiting for authorization...',
            })
        
        # Check if device now has an active session (was authorized)
        active_session = HotspotSession.objects.filter(
            router_id=router_id,
            mac_address=mac_address,
            status='active',
            expires_at__gt=timezone.now()
        ).first()
        
        if active_session:
            return Response({
                'status': 'authorized',
                'access_code': active_session.access_code,
                'message': 'Device authorized! Connecting...',
            })
        
        return Response({
            'status': 'not_found',
            'message': 'No pairing request found.',
        })
