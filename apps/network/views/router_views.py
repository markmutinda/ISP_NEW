# apps/network/views/router_views.py

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.db.models import Sum, Avg, F, Count
from django.http import HttpResponse
from rest_framework import serializers
import json
import logging
import socket

from apps.network.models.router_models import Router, RouterEvent
from apps.network.serializers.router_serializers import RouterSerializer, RouterEventSerializer
from apps.core.permissions import HasCompanyAccess
import apps.network.integrations.mikrotik_api as mikrotik_api_module

logger = logging.getLogger(__name__)


class RouterViewSet(viewsets.ModelViewSet):
    serializer_class = RouterSerializer
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['router_type', 'status', 'is_active', 'company']
    search_fields = ['name', 'ip_address', 'model', 'location', 'tags']
    ordering_fields = ['name', 'last_seen', 'created_at', 'status']

    queryset = Router.objects.all() 
    
    def get_queryset(self):
        user = self.request.user
        qs = Router.objects.all().select_related('company')
        
        # Superusers can see all routers or filter by company
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return qs.filter(company_id=company_id)
            return qs
        
        # Non-superusers only see routers in their company
        if hasattr(user, 'company') and user.company:
            return qs.filter(company=user.company)
        
        # Users without company see nothing
        return qs.none()

    def perform_create(self, serializer):
        user = self.request.user
        
        # For superusers, allow setting any company
        if user.is_superuser:
            company = serializer.validated_data.get('company')
            if not company:
                # Auto-assign first company if none specified
                from apps.core.models import Company
                company = Company.objects.first()
                if company:
                    serializer.save(company=company)
                else:
                    raise serializers.ValidationError("No companies exist. Please create a company first.")
            else:
                serializer.save()
        else:
            # Non-superusers: auto-assign their company
            if hasattr(user, 'company') and user.company:
                serializer.save(company=user.company)
            else:
                raise serializers.ValidationError("User has no company assigned")

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
        }
        
        # Add SLA stats if field exists
        if hasattr(Router, 'sla_target'):
            below_sla = qs.filter(
                uptime_percentage__lt=F('sla_target'),
                uptime_percentage__gt=0
            ).count()
            stats["below_sla_count"] = below_sla
        
        # Add authentication stats
        stats.update({
            "authenticated_routers": qs.filter(is_authenticated=True).count(),
            "pending_authentication": qs.filter(is_authenticated=False, auth_key__isnull=False).count(),
        })
        
        return Response(stats)

    @action(detail=True, methods=['get'])
    def events(self, request, pk=None):
        router = self.get_object()
        events = router.events.all().order_by('-created_at')[:50]  # Limit to 50 events
        serializer = RouterEventSerializer(events, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def users(self, request, pk=None):
        router = self.get_object()
        try:
            hotspot_active = router.hotspot_users.filter(status='ACTIVE').count()
            pppoe_connected = router.pppoe_users.filter(status='CONNECTED').count()
            return Response({
                "hotspot_users": hotspot_active,
                "pppoe_users": pppoe_connected,
                "total": hotspot_active + pppoe_connected,
            })
        except Exception as e:
            logger.error(f"Error getting users for router {router.id}: {e}")
            return Response({
                "hotspot_users": 0,
                "pppoe_users": 0,
                "total": 0,
                "error": "Could not retrieve user counts"
            })

    @action(detail=True, methods=['post'])
    def test_connection(self, request, pk=None):
        router = self.get_object()
        if not router.ip_address:
            return Response({"error": "Router has no IP address configured"}, status=400)
        
        try:
            # Try to resolve hostname
            socket.gethostbyname(router.ip_address)
            
            # Try to connect to API port
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((router.ip_address, router.api_port or 8728))
            sock.close()
            
            if result == 0:
                return Response({"status": "success", "message": "Router is reachable"})
            else:
                return Response({"error": f"Port {router.api_port} is not open"}, status=400)
        except socket.gaierror:
            return Response({"error": "Router hostname cannot be resolved"}, status=400)
        except Exception as e:
            return Response({"error": f"Connection failed: {str(e)}"}, status=400)

    @action(detail=True, methods=['post'])
    def reboot(self, request, pk=None):
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "Reboot only supported for Mikrotik routers"}, status=400)
        
        # Check if API credentials are set
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            # Import MikrotikAPI locally to avoid circular imports
            from apps.network.integrations.mikrotik_api import MikrotikAPI
            api = MikrotikAPI(router)
            if not api.connect():
                raise Exception("Failed to connect to router")
            
            api.reboot_device()
            api.disconnect()
            
            RouterEvent.objects.create(
                router=router, 
                event_type='reboot', 
                message="Reboot command sent via API"
            )
            
            return Response({"status": "success", "message": "Reboot command sent"})
        except Exception as e:
            logger.error(f"Router {router.name} reboot failed: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'])
    def maintenance(self, request, pk=None):
        router = self.get_object()
        new_status = 'maintenance' if router.status != 'maintenance' else 'online'
        old_status = router.status
        router.status = new_status
        router.save(update_fields=['status'])
        
        RouterEvent.objects.create(
            router=router, 
            event_type='maintenance', 
            message=f"Status changed from {old_status} to {new_status}"
        )
        
        return Response({"status": "success", "new_status": new_status})

    @action(detail=True, methods=['post'])
    def sync_users(self, request, pk=None):
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "User sync only supported for Mikrotik"}, status=400)
        
        # Check if API credentials are set
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            from apps.network.integrations.mikrotik_api import MikrotikAPI
            api = MikrotikAPI(router)
            if not api.connect():
                raise Exception("Failed to connect to router")
            
            # Get users (simplified - you'll need to adjust based on your MikrotikAPI implementation)
            hotspot_data = api.get_hotspot_users() if hasattr(api, 'get_hotspot_users') else []
            pppoe_data = api.get_pppoe_users() if hasattr(api, 'get_pppoe_users') else []
            
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
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'])
    def backup(self, request, pk=None):
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "Backup only supported for Mikrotik"}, status=400)
        
        # Check if API credentials are set
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            from apps.network.integrations.mikrotik_api import MikrotikAPI
            api = MikrotikAPI(router)
            if not api.connect():
                raise Exception("Failed to connect")
            
            result = api.backup_config() if hasattr(api, 'backup_config') else "Backup initiated"
            api.disconnect()
            
            RouterEvent.objects.create(
                router=router, 
                event_type='config_change', 
                message="Configuration backup created"
            )
            
            return Response({"status": "success", "message": result})
        except Exception as e:
            logger.error(f"Backup failed for {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'])
    def regenerate_auth_key(self, request, pk=None):
        router = self.get_object()
        from apps.network.models.router_models import generate_auth_key
        router.auth_key = generate_auth_key()
        router.is_authenticated = False
        router.authenticated_at = None
        router.save(update_fields=['auth_key', 'is_authenticated', 'authenticated_at'])
        
        RouterEvent.objects.create(
            router=router, 
            event_type='auth_key_regen', 
            message="Authentication key regenerated"
        )
        
        return Response({"status": "success", "new_auth_key": router.auth_key})
    
    @action(detail=True, methods=['get'], url_path='script', permission_classes=[AllowAny])
    def script(self, request, pk=None):
        """Public endpoint for router to download script"""
        router = Router.objects.filter(id=pk).first()
        if not router:
            return Response({"error": "Router not found"}, status=404)
        
        # Get version from query params
        version = request.query_params.get('version', '7')
        
        # Simple script
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
            return Response({"error": "Router not found"}, status=404)
        
        # Verify auth_key
        auth_key = request.query_params.get('auth_key')
        if not auth_key or auth_key != router.auth_key:
            return Response({"error": "Invalid auth key"}, status=401)
        
        version = request.query_params.get('version', '7')
        
        # Configuration script
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

# Basic configuration
:put "Configuring router...";

# Create bridge if it doesn't exist
:if ([:len [/interface bridge find name=bridge-local]] = 0) do={{
    /interface bridge add name=bridge-local comment="YourISP Local Bridge";
}};

# Add IP address if it doesn't exist
:if ([:len [/ip address find interface=bridge-local address=192.168.88.1/24]] = 0) do={{
    /ip address add address=192.168.88.1/24 interface=bridge-local;
}};

# Create DHCP pool if it doesn't exist
:if ([:len [/ip pool find name=dhcp-pool]] = 0) do={{
    /ip pool add name=dhcp-pool ranges=192.168.88.10-192.168.88.254;
}};

# Create DHCP server if it doesn't exist
:if ([:len [/ip dhcp-server find name=dhcp-server]] = 0) do={{
    /ip dhcp-server add interface=bridge-local name=dhcp-server address-pool=dhcp-pool lease-time=1d disabled=no;
}};

# Create DHCP network if it doesn't exist
:if ([:len [/ip dhcp-server network find address=192.168.88.0/24]] = 0) do={{
    /ip dhcp-server network add address=192.168.88.0/24 gateway=192.168.88.1 dns-server=8.8.8.8;
}};

# Create NAT rule if it doesn't exist
:if ([:len [/ip firewall nat find chain=srcnat action=masquerade out-interface-list=WAN]] = 0) do={{
    /ip firewall nat add chain=srcnat action=masquerade out-interface-list=WAN;
}};

:put "Basic configuration completed!";

# RADIUS Configuration (if shared secret exists)
:if ([:len "{router.shared_secret}"] > 0) do={{
    :put "Configuring RADIUS...";
    :if ([:len [/radius find address=camden-convocative-oversorrowfully.ngrok-free.dev]] = 0) do={{
        /radius add address=camden-convocative-oversorrowfully.ngrok-free.dev secret={router.shared_secret} service=hotspot timeout=3s;
    }};
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
        
        # Simple one-liner
        one_liner = f'/tool fetch url="https://camden-convocative-oversorrowfully.ngrok-free.dev/api/v1/network/routers/{router.id}/config/?auth_key={router.auth_key}" dst-path=yourisp.rsc; :delay 2s; /import yourisp.rsc;'
        
        return Response({
            'auth_key': router.auth_key,
            'one_liner': one_liner,
            'is_authenticated': router.is_authenticated,
            'authenticated_at': router.authenticated_at,
            'shared_secret': router.shared_secret,
        })


class RouterAuthenticateView(APIView):
    """Public endpoint for routers to authenticate"""
    permission_classes = [AllowAny]
    
    def post(self, request):
        try:
            data = request.data
            
            # Get auth_key
            auth_key = data.get('auth_key')
            if not auth_key:
                return Response({"error": "Missing auth_key"}, status=400)
            
            # Find router
            router = Router.objects.filter(auth_key=auth_key).first()
            if not router:
                return Response({"error": "Invalid authentication key"}, status=404)
            
            # Get IP address
            x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
            if x_forwarded_for:
                ip = x_forwarded_for.split(',')[0].strip()
            else:
                ip = request.META.get('REMOTE_ADDR', 'Unknown')
            
            # Update router
            router.ip_address = ip
            router.mac_address = data.get('mac', 'Unknown')
            router.firmware_version = data.get('version', 'Unknown')
            router.model = data.get('model', 'Unknown')
            router.is_authenticated = True
            router.authenticated_at = timezone.now()
            router.status = "online"
            router.last_seen = timezone.now()
            router.save()
            
            # Create event
            RouterEvent.objects.create(
                router=router,
                event_type="auth_success",
                message=f"Router authenticated from {ip}",
            )
            
            return Response({
                "status": "success",
                "message": "Router authenticated successfully",
                "router_id": router.id,
                "router_name": router.name,
            })
            
        except Exception as e:
            logger.error(f"Router authentication error: {str(e)}")
            return Response({"error": "Internal server error"}, status=500)


class RouterHeartbeatView(APIView):
    """Public endpoint for router heartbeats"""
    permission_classes = [AllowAny]
    
    def post(self, request):
        try:
            data = request.data
            auth_key = data.get('auth_key') or data.get('key')
            
            if not auth_key:
                return Response({"error": "Missing auth_key"}, status=400)
            
            router = Router.objects.filter(auth_key=auth_key).first()
            if not router:
                return Response({"error": "Invalid key"}, status=404)
            
            # Update heartbeat
            router.last_seen = timezone.now()
            router.status = 'online'
            
            # Optional: Update IP if provided
            if 'ip' in data:
                router.ip_address = data['ip']
                router.save(update_fields=['last_seen', 'status', 'ip_address'])
            else:
                router.save(update_fields=['last_seen', 'status'])
            
            logger.debug(f"Heartbeat from router {router.name} (ID: {router.id})")
            
            return Response({"status": "ok", "router_id": router.id})
            
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
            return Response({"error": str(e)}, status=400)