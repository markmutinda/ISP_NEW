# apps/network/views/router_views.py

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.parsers import JSONParser
import json
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.db.models import Sum, Avg, F
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from apps.network.models.router_models import (
    Router,
    RouterEvent,
)

from apps.network.serializers.router_serializers import (
    RouterSerializer,
    RouterEventSerializer,
)

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
    
    @action(detail=True, methods=['get'], url_path='script', permission_classes=[AllowAny])
    def script(self, request, pk=None):
        """Public endpoint for router to download script"""
        router = Router.objects.filter(id=pk).first()
        if not router:
            return Response({"error": "Router not found"}, status=status.HTTP_404_NOT_FOUND)
        
        # Get version from query params
        version = request.query_params.get('version', '7')
        
        # SUPER SIMPLE script - just runs the config directly
        script_content = f"""# YourISP Configuration Script for Router ID: {router.id}
# This script will configure your router for YourISP service

:put "Starting YourISP configuration...";

# Download and run configuration
/tool fetch url="https://camden-convocative-oversorrowfully.ngrok-free.dev/api/v1/network/routers/{router.id}/config/?auth_key={router.auth_key}&version={version}" dst-path=yourisp-config.rsc;
:delay 2s;
/import yourisp-config.rsc;

:put "Configuration completed!";
"""
        
        response = HttpResponse(script_content, content_type='text/plain')
        response['Content-Disposition'] = f'attachment; filename="yourisp-{router.id}.rsc"'
        return response
    
    @action(detail=True, methods=['get'], url_path='config', permission_classes=[AllowAny])
    def config_script(self, request, pk=None):
        """Public endpoint for router to download configuration script"""
        router = Router.objects.filter(id=pk).first()
        if not router:
            return Response({"error": "Router not found"}, status=status.HTTP_404_NOT_FOUND)
        
        # Verify auth_key
        auth_key = request.query_params.get('auth_key')
        if not auth_key or auth_key != router.auth_key:
            return Response({"error": "Invalid auth key"}, status=status.HTTP_401_UNAUTHORIZED)
        
        version = request.query_params.get('version', '7')
        
        # UPDATED VERSION - Gets model and sends it to auth endpoint
        script_content = f"""# YourISP Configuration for {router.name}
# Generated at {timezone.now()}

:put "Starting YourISP configuration...";

# Get router information
:local macAddr "";
:local ethernetList [/interface ethernet find];
:if ([:len $ethernetList] > 0) do={{
    :set macAddr [/interface ethernet get [:pick $ethernetList 0] mac-address];
}} else={{
    :set macAddr "00:00:00:00:00:00";
}};

:local routerModel "";
:local routerBoardInfo [/system routerboard print];
:if ([:len $routerBoardInfo] > 0) do={{
    :set routerModel [/system routerboard get model];
}} else={{
    :set routerModel "Unknown";
}};

:local routerIdentity [/system identity get name];
:local routerVersion [/system resource get version];

:put ("MAC Address: $macAddr");
:put ("Router Model: $routerModel");
:put ("Router Identity: $routerIdentity");
:put ("Router Version: $routerVersion");

# Register router with YourISP
:put "Registering router...";
/tool fetch url="https://camden-convocative-oversorrowfully.ngrok-free.dev/api/v1/network/routers/auth/" \\
  http-method=post \\
  http-header-field="Content-Type: application/json" \\
  http-data="{{\\"auth_key\\":\\"{router.auth_key}\\",\\"mac\\":\\"$macAddr\\",\\"model\\":\\"$routerModel\\",\\"identity\\":\\"$routerIdentity\\",\\"version\\":\\"$routerVersion\\"}}";

:delay 2s;

# Basic configuration - WITH ERROR HANDLING
:put "Configuring router...";

# Check if bridge already exists
:do {{
    /interface bridge add name=bridge-local comment="YourISP Local Bridge";
}} on-error={{ :put "Bridge already exists or error: $error"; }}

# Check if IP address already exists
:do {{
    /ip address add address=192.168.88.1/24 interface=bridge-local;
}} on-error={{ :put "IP address already exists or error: $error"; }}

# Check if DHCP pool exists
:do {{
    /ip pool add name=dhcp-pool ranges=192.168.88.10-192.168.88.254;
}} on-error={{ :put "DHCP pool already exists or error: $error"; }}

# Check if DHCP server exists
:do {{
    /ip dhcp-server add interface=bridge-local name=dhcp-server address-pool=dhcp-pool lease-time=1d disabled=no;
}} on-error={{ :put "DHCP server already exists or error: $error"; }}

# Check if DHCP network exists
:do {{
    /ip dhcp-server network add address=192.168.88.0/24 gateway=192.168.88.1 dns-server=8.8.8.8;
}} on-error={{ :put "DHCP network already exists or error: $error"; }}

# Check if NAT rule exists
:do {{
    /ip firewall nat add chain=srcnat action=masquerade out-interface-list=WAN;
}} on-error={{ :put "NAT rule already exists or error: $error"; }}

:put "Basic configuration completed!";

# RADIUS Configuration (if shared secret exists)
:if ([:len "{router.shared_secret}"] > 0) do={{
    :put "Configuring RADIUS...";
    :do {{
        /radius add address=camden-convocative-oversorrowfully.ngrok-free.dev secret={router.shared_secret} service=hotspot timeout=3s;
    }} on-error={{ :put "RADIUS already exists or error: $error"; }};
    :put "RADIUS configured.";
}};

:put "YourISP configuration completed successfully!";
:put "Router is now ready for service.";
"""
        
        response = HttpResponse(script_content, content_type='text/plain')
        response['Content-Disposition'] = f'attachment; filename="yourisp-config-{router.id}.rsc"'
        return response

    @action(detail=True, methods=['get'], url_path='auth-key')
    def auth_key(self, request, pk=None):
        router = self.get_object()
        
        # SIMPLE one-liner like Lipa Net
        one_liner = f'/tool fetch url="https://camden-convocative-oversorrowfully.ngrok-free.dev/api/v1/network/routers/{router.id}/config/?auth_key={router.auth_key}" dst-path=yourisp.rsc; :delay 2s; /import yourisp.rsc;'
        
        return Response({
            'auth_key': router.auth_key,
            'one_liner': one_liner,
            'is_authenticated': router.is_authenticated,
            'authenticated_at': router.authenticated_at,
        })

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return queryset.filter(company_id=company_id)
            return queryset
        
        if hasattr(user, 'company') and user.company:
            return queryset.filter(company=user.company)
        
        return queryset.none()

# SIMPLE AUTHENTICATION ENDPOINT
# SIMPLE AUTHENTICATION ENDPOINT
class RouterAuthenticateView(APIView):
    permission_classes = [AllowAny]
    
    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)
    
    def post(self, request):
        try:
            # Parse JSON from request body
            if not request.body:
                return Response({"error": "Empty request body"}, status=status.HTTP_400_BAD_REQUEST)
            
            try:
                data = json.loads(request.body.decode('utf-8'))
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error: {e}")
                return Response({"error": "Invalid JSON format"}, status=status.HTTP_400_BAD_REQUEST)
            
            # Get auth_key
            auth_key = data.get('auth_key')
            if not auth_key:
                return Response({"error": "Missing auth_key"}, status=status.HTTP_400_BAD_REQUEST)
            
            # Find router
            router = Router.objects.filter(auth_key=auth_key).first()
            if not router:
                logger.warning(f"Invalid auth_key provided: {auth_key}")
                return Response({"error": "Invalid authentication key"}, status=status.HTTP_404_NOT_FOUND)
            
            # Get IP address
            x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
            if x_forwarded_for:
                ip = x_forwarded_for.split(',')[0].strip()
            else:
                ip = request.META.get('REMOTE_ADDR', 'Unknown')
            
            # Get other data
            mac = data.get('mac', 'Unknown')
            identity = data.get('identity', 'Unknown')
            version = data.get('version', 'Unknown')
            model = data.get('model', 'Unknown')
            
            # Log the authentication
            logger.info(f"Router {router.name} (ID: {router.id}) authenticated from IP: {ip}")
            logger.info(f"Router Details - Model: {model}, Version: {version}, MAC: {mac}")
            
            # Update router with ALL details
            router.ip_address = ip
            router.mac_address = mac  # Store MAC address
            router.firmware_version = version  # Store firmware version
            router.model = model  # Store model
            router.is_authenticated = True
            router.authenticated_at = timezone.now()
            router.status = "online"
            router.last_seen = timezone.now()
            router.save()
            
            # Create event
            try:
                RouterEvent.objects.create(
                    router=router,
                    event_type="authenticated",
                    message=f"Router authenticated from IP {ip}",
                    details={
                        "mac_address": mac,
                        "identity": identity,
                        "version": version,
                        "model": model
                    }
                )
            except TypeError:
                # If details field doesn't exist in model
                RouterEvent.objects.create(
                    router=router,
                    event_type="authenticated",
                    message=f"Router authenticated from IP {ip} (Model: {model}, Version: {version}, MAC: {mac})",
                )
            
            return Response({
                "status": "success",
                "message": "Router authenticated successfully",
                "router_id": router.id,
                "router_name": router.name,
                "ip_address": ip,
                "model": model,
                "firmware_version": version
            })
            
        except Exception as e:
            logger.error(f"Authentication error: {str(e)}")
            return Response({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class RouterHeartbeatView(APIView):
    permission_classes = [AllowAny]
    
    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)
    
    def post(self, request):
        try:
            # Try to parse JSON
            if request.body:
                try:
                    data = json.loads(request.body.decode('utf-8'))
                except:
                    data = {}
            else:
                data = request.data if hasattr(request, 'data') else {}
            
            # Get key from various fields
            key = None
            for field in ['key', 'auth_key', 'authKey', 'token']:
                if field in data:
                    key = data.get(field)
                    break
            
            if not key:
                return Response({"error": "Missing key"}, status=status.HTTP_400_BAD_REQUEST)
            
            router = Router.objects.filter(auth_key=key).first()
            if not router:
                return Response({"error": "Invalid key"}, status=status.HTTP_404_NOT_FOUND)
            
            # Optionally update IP if provided
            if 'ip' in data:
                router.ip_address = data['ip']
                update_fields = ['last_seen', 'status', 'ip_address']
            else:
                update_fields = ['last_seen', 'status']
            
            router.last_seen = timezone.now()
            router.status = 'online'
            router.save(update_fields=update_fields)
            
            logger.debug(f"Heartbeat from router {router.name} (ID: {router.id})")
            
            return Response({"status": "ok", "router_id": router.id})
            
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)