# apps/network/views/router_views.py

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.db.models import Sum, Avg, F

from apps.network.models.router_models import (
    Router,
    RouterEvent,
    MikrotikInterface,
    HotspotUser,
    PPPoEUser,
    MikrotikQueue,
)

from apps.network.serializers.router_serializers import (
    RouterSerializer,
    RouterEventSerializer,
)

# NOTE: We no longer need sub-serializers in router_serializers.py
# If you want to keep granular endpoints, create a separate file later
# For now, we remove them to avoid import errors

from apps.core.permissions import HasCompanyAccess
from apps.network.integrations.mikrotik_api import MikrotikAPI
import logging
import socket

logger = logging.getLogger(__name__)


class RouterViewSet(viewsets.ModelViewSet):
    queryset = Router.objects.all().select_related('company')
    serializer_class = RouterSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['company', 'router_type', 'status', 'is_active']
    search_fields = ['name', 'ip_address', 'model', 'location', 'tags']
    ordering_fields = ['name', 'last_seen', 'created_at']

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return Router.objects.all()
        return Router.objects.filter(company__in=user.companies.all())

    @action(detail=False, methods=['get'])
    def dashboard_stats(self, request):
        qs = self.get_queryset()
        stats = {
            "total_routers": qs.count(),
            "online_routers": qs.filter(status='online').count(),
            "offline_routers": qs.filter(status='offline').count(),
            "warning_routers": qs.filter(status='warning').count(),
            "maintenance_routers": qs.filter(status='maintenance').count(),
            "total_connected_users": qs.aggregate(total=Sum('active_users'))['total'] or 0,
            "average_uptime": round(qs.aggregate(avg=Avg('uptime_percentage'))['avg'] or 0, 2),
            "below_sla_count": qs.filter(uptime_percentage__lt=F('sla_target')).count(),
        }
        return Response(stats)

    @action(detail=True, methods=['get'])
    def events(self, request, pk=None):
        router = self.get_object()
        events = router.events.all().order_by('-created_at')
        serializer = RouterEventSerializer(events, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def users(self, request, pk=None):
        router = self.get_object()
        hotspot_active = router.hotspot_users.filter(status='ACTIVE').count()
        pppoe_connected = router.pppoe_users.filter(status='CONNECTED').count()
        return Response({
            "hotspot_users": hotspot_active,
            "pppoe_users": pppoe_connected,
            "total": hotspot_active + pppoe_connected,
        })

    @action(detail=True, methods=['post'])
    def test_connection(self, request, pk=None):
        router = self.get_object()
        if not router.ip_address:
            return Response({"error": "Router has no IP address configured"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            socket.gethostbyname(router.ip_address)
            return Response({"status": "success", "message": "Router is reachable"})
        except socket.gaierror:
            return Response({"error": "Router is unreachable"}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def reboot(self, request, pk=None):
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "Reboot only supported for Mikrotik routers"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            api = MikrotikAPI(router)
            if not api.connect():
                raise Exception("Failed to connect to router")
            api.reboot_device()
            api.disconnect()
            RouterEvent.objects.create(router=router, event_type='reboot', message="Reboot command sent via API")
            return Response({"status": "success", "message": "Reboot command sent"})
        except Exception as e:
            logger.error(f"Router {router.name} reboot failed: {str(e)}")
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def maintenance(self, request, pk=None):
        router = self.get_object()
        new_status = 'maintenance' if router.status != 'maintenance' else 'online'
        old_status = router.status
        router.status = new_status
        router.save(update_fields=['status'])
        RouterEvent.objects.create(router=router, event_type='maintenance', message=f"Status changed from {old_status} to {new_status}")
        return Response({"status": "success", "new_status": new_status})

    @action(detail=True, methods=['post'])
    def sync_users(self, request, pk=None):
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "User sync only supported for Mikrotik"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            api = MikrotikAPI(router)
            if not api.connect():
                raise Exception("Failed to connect to router")
            hotspot_data = api.get_hotspot_users()
            pppoe_data = api.get_pppoe_users()
            hotspot_active = sum(1 for u in hotspot_data if not u.get('disabled', False))
            pppoe_active = sum(1 for u in pppoe_data if not u.get('disabled', False))
            router.total_users = len(hotspot_data) + len(pppoe_data)
            router.active_users = hotspot_active + pppoe_active
            router.last_seen = timezone.now()
            router.status = 'online'
            router.save(update_fields=['total_users', 'active_users', 'last_seen', 'status'])
            api.disconnect()
            return Response({
                "status": "success",
                "hotspot_synced": len(hotspot_data),
                "pppoe_synced": len(pppoe_data),
                "active_users": router.active_users,
            })
        except Exception as e:
            logger.error(f"User sync failed for {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def backup(self, request, pk=None):
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "Backup only supported for Mikrotik"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            api = MikrotikAPI(router)
            if not api.connect():
                raise Exception("Failed to connect")
            result = api.backup_config()
            api.disconnect()
            RouterEvent.objects.create(router=router, event_type='config_change', message="Configuration backup created")
            return Response({"status": "success", "message": result})
        except Exception as e:
            logger.error(f"Backup failed for {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def auth_key(self, request, pk=None):
        router = self.get_object()
        return Response({"auth_key": router.auth_key})

    @action(detail=True, methods=['post'])
    def regenerate_auth_key(self, request, pk=None):
        router = self.get_object()
        from apps.network.models.router_models import generate_auth_key
        router.auth_key = generate_auth_key()
        router.is_authenticated = False
        router.authenticated_at = None
        router.save(update_fields=['auth_key', 'is_authenticated', 'authenticated_at'])
        RouterEvent.objects.create(router=router, event_type='warning', message="Authentication key regenerated")
        return Response({"status": "success", "new_auth_key": router.auth_key})


# PUBLIC ENDPOINTS
class RouterAuthenticateView(APIView):
    permission_classes = []

    def get(self, request):
        key = request.query_params.get('key')
        if not key:
            return Response({"error": "Missing key parameter"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            router = Router.objects.get(auth_key=key)
        except Router.DoesNotExist:
            return Response({"error": "Invalid or expired key"}, status=status.HTTP_404_NOT_FOUND)

        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        ip = x_forwarded_for.split(',')[0].strip() if x_forwarded_for else request.META.get('REMOTE_ADDR')

        router.ip_address = ip
        router.is_authenticated = True
        router.authenticated_at = timezone.now()
        router.status = 'online'
        router.last_seen = timezone.now()
        router.save(update_fields=['ip_address', 'is_authenticated', 'authenticated_at', 'status', 'last_seen'])

        RouterEvent.objects.create(router=router, event_type='up', message=f"Router authenticated from IP {ip}")

        return Response({
            "status": "success",
            "message": f"Router {router.name} authenticated successfully",
            "router_id": router.id,
        })


class RouterHeartbeatView(APIView):
    permission_classes = []

    def post(self, request):
        key = request.data.get('key')
        if not key:
            return Response({"error": "Missing key"}, status=status.HTTP_400_BAD_REQUEST)
        router = Router.objects.filter(auth_key=key).first()
        if not router:
            return Response({"error": "Invalid key"}, status=status.HTTP_404_NOT_FOUND)
        router.last_seen = timezone.now()
        router.status = 'online'
        router.save(update_fields=['last_seen', 'status'])
        return Response({"status": "ok"})


# REMOVED: Granular ViewSets (MikrotikInterfaceViewSet, etc.)
# We removed them because:
# 1. Their serializers don't exist anymore
# 2. The frontend only needs the main Router endpoints
# 3. You can add them back later if needed