import hmac
import ipaddress

from django.conf import settings
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import GlobalRouterMap


class OpenVPNAuthView(APIView):
    """
    Called ONLY by the openvpn container (auth-user-pass-verify).
    username = router VPN IP, password = router shared_secret (from public GlobalRouterMap).
    One indexed public-schema query per (re)connect.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = []

    def post(self, request):
        token = getattr(settings, 'OPENVPN_AUTH_TOKEN', '')
        supplied = request.headers.get('X-VPN-Webhook-Secret', '')
        if not token or not hmac.compare_digest(supplied.encode(), token.encode()):
            return Response(status=403)

        username = str(request.data.get('username') or '').strip()
        password = str(request.data.get('password') or '')
        try:
            ip = ipaddress.ip_address(username)
            net = ipaddress.ip_network(
                getattr(settings, 'OPENVPN_NETWORK_CIDR', '10.9.0.0/16'), strict=False
            )
        except ValueError:
            return Response(status=401)
        if ip not in net:
            return Response(status=401)

        secret = (
            GlobalRouterMap.objects.filter(nas_ip=str(ip), is_active=True)
            .values_list('nas_secret', flat=True).first()
        )
        if secret and hmac.compare_digest(str(secret).encode(), password.encode()):
            return Response({'ok': True})
        return Response(status=401)