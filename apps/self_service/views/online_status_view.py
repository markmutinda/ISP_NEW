from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from ..permissions import CustomerOnlyPermission
from apps.radius.models import RadAcct
from apps.radius.services.usage_service import exclude_walled_sessions


class CustomerOnlineStatusView(APIView):
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]

    def get(self, request):
        customer = request.user.customer_profile
        radius_username = None
        if hasattr(customer, 'radius_credentials'):
            radius_username = customer.radius_credentials.username

        session = None
        if radius_username:
            # Walled PPPoE users (expired subscription, 512k captive-portal
            # session) hold a live radacct row but aren't really online —
            # exclude_walled_sessions drops them so the dashboard badge
            # correctly shows "Offline" until they renew.
            session = exclude_walled_sessions(
                RadAcct.objects.filter(
                    username=radius_username,
                    acctstoptime__isnull=True
                )
            ).first()

        return Response({
            'is_online': session is not None,
            'ip_address': session.framedipaddress if session else None,
        })