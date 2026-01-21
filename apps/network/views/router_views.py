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
    filterset_fields = ['router_type', 'status', 'is_active']
    search_fields = ['name', 'ip_address', 'model', 'location', 'tags']
    ordering_fields = ['name', 'last_seen', 'created_at', 'status']

    queryset = Router.objects.all() 
    
    def get_queryset(self):
        user = self.request.user
        
        # All users in a tenant see only their tenant's routers
        qs = Router.objects.all()
        
        # Filter by tenant_subdomain if available
        if hasattr(self.request, 'tenant') and self.request.tenant:
            qs = qs.filter(tenant_subdomain=self.request.tenant.subdomain)
        
        return qs

    def perform_create(self, serializer):
        # The serializer will handle adding company_name and tenant_subdomain
        # We just need to ensure the context has the request
        serializer.save()
        
    # Optional: Add this method to debug the request
    def create(self, request, *args, **kwargs):
        logger.debug(f"Create router - Request has company: {hasattr(request, 'company')}")
        logger.debug(f"Create router - Request has tenant: {hasattr(request, 'tenant')}")
        if hasattr(request, 'company'):
            logger.debug(f"Create router - Company: {request.company}")
        if hasattr(request, 'tenant'):
            logger.debug(f"Create router - Tenant: {request.tenant}")
        
        return super().create(request, *args, **kwargs)

    # ────────────────────────────────────────────────────────────────
    # MIKROTIK API ENDPOINTS - LIVE STATUS & HEALTH
    # ────────────────────────────────────────────────────────────────

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def live_status(self, request, pk=None):
        """Get real-time router status"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            status = api.get_live_status()
            return Response(status)
        except Exception as e:
            logger.error(f"Failed to get live status for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def system_health(self, request, pk=None):
        """Get comprehensive system health information"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            health = api.get_system_health()
            return Response(health)
        except Exception as e:
            logger.error(f"Failed to get system health for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def sync_device_info(self, request, pk=None):
        """Sync device information from Mikrotik"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            device_info = api.sync_device_info()
            
            # Update router model with synced data if needed
            router.model = device_info.get('model', router.model)
            router.firmware_version = device_info.get('firmware_version', router.firmware_version)
            router.save(update_fields=['model', 'firmware_version'])
            
            return Response(device_info)
        except Exception as e:
            logger.error(f"Failed to sync device info for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    # ────────────────────────────────────────────────────────────────
    # MIKROTIK API ENDPOINTS - CONNECTED USERS
    # ────────────────────────────────────────────────────────────────

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def active_hotspot_users(self, request, pk=None):
        """Get currently connected hotspot users"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            users = api.get_active_hotspot_users()
            return Response(users)
        except Exception as e:
            logger.error(f"Failed to get active hotspot users for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def active_pppoe_sessions(self, request, pk=None):
        """Get active PPPoE sessions"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            sessions = api.get_active_pppoe_sessions()
            return Response(sessions)
        except Exception as e:
            logger.error(f"Failed to get active PPPoE sessions for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def hotspot_users(self, request, pk=None):
        """Get all hotspot users"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            users = api.get_hotspot_users()
            return Response(users)
        except Exception as e:
            logger.error(f"Failed to get hotspot users for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def hotspot_user_stats(self, request, pk=None):
        """Get hotspot user active session stats"""
        router = self.get_object()
        username = request.query_params.get('username')
        
        if not username:
            return Response({"error": "Username parameter is required"}, status=400)
        
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            stats = api.get_hotspot_user_stats(username)
            return Response(stats if stats else {"error": "User not found or not active"})
        except Exception as e:
            logger.error(f"Failed to get hotspot user stats for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def pppoe_users(self, request, pk=None):
        """Get all PPPoE users"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            users = api.get_pppoe_users()
            return Response(users)
        except Exception as e:
            logger.error(f"Failed to get PPPoE users for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def pppoe_user_stats(self, request, pk=None):
        """Get PPPoE user active session stats"""
        router = self.get_object()
        username = request.query_params.get('username')
        
        if not username:
            return Response({"error": "Username parameter is required"}, status=400)
        
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            stats = api.get_pppoe_user_stats(username)
            return Response(stats)
        except Exception as e:
            logger.error(f"Failed to get PPPoE user stats for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    # ────────────────────────────────────────────────────────────────
    # MIKROTIK API ENDPOINTS - USER MANAGEMENT
    # ────────────────────────────────────────────────────────────────

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def create_hotspot_user(self, request, pk=None):
        """Create hotspot user"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        username = request.data.get('username')
        password = request.data.get('password')
        profile = request.data.get('profile', 'default')
        limit_uptime = request.data.get('limit_uptime', '')
        limit_bytes = request.data.get('limit_bytes', '')
        
        if not username or not password:
            return Response({"error": "Username and password are required"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            success = api.create_hotspot_user(username, password, profile, limit_uptime, limit_bytes)
            
            if success:
                RouterEvent.objects.create(
                    router=router,
                    event_type='user_created',
                    message=f"Hotspot user {username} created"
                )
                return Response({"status": "success", "message": "Hotspot user created"})
            else:
                return Response({"error": "Failed to create hotspot user"}, status=400)
        except Exception as e:
            logger.error(f"Failed to create hotspot user for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def create_pppoe_user(self, request, pk=None):
        """Create PPPoE user"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        username = request.data.get('username')
        password = request.data.get('password')
        profile = request.data.get('profile', 'default-encryption')
        local_address = request.data.get('local_address', '')
        remote_address = request.data.get('remote_address', '')
        
        if not username or not password:
            return Response({"error": "Username and password are required"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            success = api.create_pppoe_user(username, password, profile, local_address, remote_address)
            
            if success:
                RouterEvent.objects.create(
                    router=router,
                    event_type='user_created',
                    message=f"PPPoE user {username} created"
                )
                return Response({"status": "success", "message": "PPPoE user created"})
            else:
                return Response({"error": "Failed to create PPPoE user"}, status=400)
        except Exception as e:
            logger.error(f"Failed to create PPPoE user for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def enable_hotspot_user(self, request, pk=None):
        """Enable hotspot user"""
        router = self.get_object()
        username = request.data.get('username')
        
        if not username:
            return Response({"error": "Username is required"}, status=400)
        
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            success = api.enable_hotspot_user(username)
            
            if success:
                RouterEvent.objects.create(
                    router=router,
                    event_type='user_enabled',
                    message=f"Hotspot user {username} enabled"
                )
                return Response({"status": "success", "message": f"User {username} enabled"})
            else:
                return Response({"error": f"Failed to enable user {username}"}, status=400)
        except Exception as e:
            logger.error(f"Failed to enable hotspot user for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def disable_hotspot_user(self, request, pk=None):
        """Disable hotspot user"""
        router = self.get_object()
        username = request.data.get('username')
        
        if not username:
            return Response({"error": "Username is required"}, status=400)
        
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            success = api.disable_hotspot_user(username)
            
            if success:
                RouterEvent.objects.create(
                    router=router,
                    event_type='user_disabled',
                    message=f"Hotspot user {username} disabled"
                )
                return Response({"status": "success", "message": f"User {username} disabled"})
            else:
                return Response({"error": f"Failed to disable user {username}"}, status=400)
        except Exception as e:
            logger.error(f"Failed to disable hotspot user for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    # ────────────────────────────────────────────────────────────────
    # MIKROTIK API ENDPOINTS - FIREWALL & QUEUES
    # ────────────────────────────────────────────────────────────────

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def firewall_filter_rules(self, request, pk=None):
        """Get all firewall filter rules"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            rules = api.get_firewall_filter_rules()
            return Response(rules)
        except Exception as e:
            logger.error(f"Failed to get firewall rules for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def queues(self, request, pk=None):
        """Get all queues"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            queues = api.get_queues()
            return Response(queues)
        except Exception as e:
            logger.error(f"Failed to get queues for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def add_simple_queue(self, request, pk=None):
        """Add a simple queue for rate limiting"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        name = request.data.get('name')
        target = request.data.get('target')
        max_limit = request.data.get('max_limit', '5M/5M')
        
        if not name or not target:
            return Response({"error": "Name and target are required"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            success = api.add_simple_queue(name, target, max_limit)
            
            if success:
                RouterEvent.objects.create(
                    router=router,
                    event_type='queue_created',
                    message=f"Queue {name} created for {target}"
                )
                return Response({"status": "success", "message": "Queue created"})
            else:
                return Response({"error": "Failed to create queue"}, status=400)
        except Exception as e:
            logger.error(f"Failed to add queue for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def create_queue(self, request, pk=None):
        """Create queue"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        name = request.data.get('name')
        target = request.data.get('target')
        max_limit = request.data.get('max_limit')
        burst_limit = request.data.get('burst_limit', '')
        priority = request.data.get('priority', '8')
        
        if not name or not target or not max_limit:
            return Response({"error": "Name, target and max_limit are required"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            success = api.create_queue(name, target, max_limit, burst_limit, priority)
            
            if success:
                RouterEvent.objects.create(
                    router=router,
                    event_type='queue_created',
                    message=f"Queue {name} created"
                )
                return Response({"status": "success", "message": "Queue created"})
            else:
                return Response({"error": "Failed to create queue"}, status=400)
        except Exception as e:
            logger.error(f"Failed to create queue for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def enable_queue(self, request, pk=None):
        """Enable queue"""
        router = self.get_object()
        queue_name = request.data.get('queue_name')
        
        if not queue_name:
            return Response({"error": "Queue name is required"}, status=400)
        
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            success = api.enable_queue(queue_name)
            
            if success:
                RouterEvent.objects.create(
                    router=router,
                    event_type='queue_enabled',
                    message=f"Queue {queue_name} enabled"
                )
                return Response({"status": "success", "message": f"Queue {queue_name} enabled"})
            else:
                return Response({"error": f"Failed to enable queue {queue_name}"}, status=400)
        except Exception as e:
            logger.error(f"Failed to enable queue for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def disable_queue(self, request, pk=None):
        """Disable queue"""
        router = self.get_object()
        queue_name = request.data.get('queue_name')
        
        if not queue_name:
            return Response({"error": "Queue name is required"}, status=400)
        
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            success = api.disable_queue(queue_name)
            
            if success:
                RouterEvent.objects.create(
                    router=router,
                    event_type='queue_disabled',
                    message=f"Queue {queue_name} disabled"
                )
                return Response({"status": "success", "message": f"Queue {queue_name} disabled"})
            else:
                return Response({"error": f"Failed to disable queue {queue_name}"}, status=400)
        except Exception as e:
            logger.error(f"Failed to disable queue for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def add_firewall_rule(self, request, pk=None):
        """Add firewall rule"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        chain = request.data.get('chain')
        action = request.data.get('action')
        src_address = request.data.get('src_address', '')
        dst_address = request.data.get('dst_address', '')
        protocol = request.data.get('protocol', '')
        dst_port = request.data.get('dst_port', '')
        comment = request.data.get('comment', '')
        
        if not chain or not action:
            return Response({"error": "Chain and action are required"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            success = api.add_firewall_rule(chain, action, src_address, dst_address, protocol, dst_port, comment)
            
            if success:
                RouterEvent.objects.create(
                    router=router,
                    event_type='firewall_rule_added',
                    message=f"Firewall rule added to {chain} chain"
                )
                return Response({"status": "success", "message": "Firewall rule added"})
            else:
                return Response({"error": "Failed to add firewall rule"}, status=400)
        except Exception as e:
            logger.error(f"Failed to add firewall rule for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    # ────────────────────────────────────────────────────────────────
    # MIKROTIK API ENDPOINTS - INTERFACE MANAGEMENT
    # ────────────────────────────────────────────────────────────────

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def interfaces(self, request, pk=None):
        """Get all interfaces"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            interfaces = api.get_interfaces()
            return Response(interfaces)
        except Exception as e:
            logger.error(f"Failed to get interfaces for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def enable_interface(self, request, pk=None):
        """Enable interface"""
        router = self.get_object()
        interface_name = request.data.get('interface_name')
        
        if not interface_name:
            return Response({"error": "Interface name is required"}, status=400)
        
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            success = api.enable_interface(interface_name)
            
            if success:
                RouterEvent.objects.create(
                    router=router,
                    event_type='interface_enabled',
                    message=f"Interface {interface_name} enabled"
                )
                return Response({"status": "success", "message": f"Interface {interface_name} enabled"})
            else:
                return Response({"error": f"Failed to enable interface {interface_name}"}, status=400)
        except Exception as e:
            logger.error(f"Failed to enable interface for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def disable_interface(self, request, pk=None):
        """Disable interface"""
        router = self.get_object()
        interface_name = request.data.get('interface_name')
        
        if not interface_name:
            return Response({"error": "Interface name is required"}, status=400)
        
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            success = api.disable_interface(interface_name)
            
            if success:
                RouterEvent.objects.create(
                    router=router,
                    event_type='interface_disabled',
                    message=f"Interface {interface_name} disabled"
                )
                return Response({"status": "success", "message": f"Interface {interface_name} disabled"})
            else:
                return Response({"error": f"Failed to disable interface {interface_name}"}, status=400)
        except Exception as e:
            logger.error(f"Failed to disable interface for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def interface_traffic(self, request, pk=None):
        """Get traffic statistics for specific interface"""
        router = self.get_object()
        interface_name = request.query_params.get('interface_name')
        
        if not interface_name:
            return Response({"error": "Interface name parameter is required"}, status=400)
        
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            traffic = api.get_interface_traffic(interface_name)
            return Response(traffic)
        except Exception as e:
            logger.error(f"Failed to get interface traffic for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    # ────────────────────────────────────────────────────────────────
    # MIKROTIK API ENDPOINTS - DHCP MANAGEMENT
    # ────────────────────────────────────────────────────────────────

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def dhcp_leases(self, request, pk=None):
        """Get DHCP leases"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            leases = api.get_dhcp_leases()
            return Response(leases)
        except Exception as e:
            logger.error(f"Failed to get DHCP leases for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    # ────────────────────────────────────────────────────────────────
    # MIKROTIK API ENDPOINTS - DIAGNOSTICS
    # ────────────────────────────────────────────────────────────────

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def ping(self, request, pk=None):
        """Run ping from router"""
        router = self.get_object()
        target = request.data.get('target', '8.8.8.8')
        count = request.data.get('count', 3)
        
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            result = api.ping(target, count)
            return Response(result)
        except Exception as e:
            logger.error(f"Failed to ping from router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def traceroute(self, request, pk=None):
        """Run traceroute from router"""
        router = self.get_object()
        target = request.data.get('target', '8.8.8.8')
        
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            result = api.traceroute(target)
            return Response(result)
        except Exception as e:
            logger.error(f"Failed to run traceroute from router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def system_logs(self, request, pk=None):
        """Get system logs"""
        router = self.get_object()
        lines = request.query_params.get('lines', 50)
        
        try:
            lines = int(lines)
        except ValueError:
            lines = 50
        
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            logs = api.get_system_logs(lines)
            return Response(logs)
        except Exception as e:
            logger.error(f"Failed to get system logs for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def wireless_interfaces(self, request, pk=None):
        """Get wireless interface information"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            wireless = api.get_wireless_interfaces()
            return Response(wireless)
        except Exception as e:
            logger.error(f"Failed to get wireless interfaces for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def wireless_registrations(self, request, pk=None):
        """Get wireless client registrations"""
        router = self.get_object()
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            registrations = api.get_wireless_registrations()
            return Response(registrations)
        except Exception as e:
            logger.error(f"Failed to get wireless registrations for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)

    # ────────────────────────────────────────────────────────────────
    # EXISTING ENDPOINTS (from your original code)
    # ────────────────────────────────────────────────────────────────

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
        
        # IDENTICAL SCHEMA LOGIC AS ABOVE
        from django.db import connection
        connection.set_schema_to_public()
        
        from apps.core.models import Tenant
        from apps.network.models.router_models import Router
        
        tenants = Tenant.objects.filter(is_active=True)
        router = None
        
        for tenant in tenants:
            try:
                connection.set_tenant(tenant)
                try:
                    router = Router.objects.filter(id=pk).first()
                    if router:
                        break
                except Exception:
                    continue
            except Exception:
                continue
        
        if not router:
            connection.set_schema_to_public()
            return Response({"error": "Router not found"}, status=404)
        
        # Generate simple script
        version = request.query_params.get('version', '7')
        script_content = f"""# YourISP Configuration Script for Router ID: {router.id}
# This script will configure your router for YourISP service

:put "Starting YourISP configuration...";

# Download and run configuration
/tool fetch url="https://camden-convocative-oversorrowfully.ngrok-free.dev/api/v1/network/routers/{router.id}/config/?auth_key={router.auth_key}&version={version}" dst-path=yourisp-config.rsc;
:delay 2s;
/import yourisp-config.rsc;

:put "Configuration completed!";
"""
        connection.set_schema_to_public()
        
        response = HttpResponse(script_content, content_type='text/plain')
        response['Content-Disposition'] = f'attachment; filename="yourisp-{router.id}.rsc"'
        return response
    
    @action(detail=True, methods=['get'], url_path='config', permission_classes=[AllowAny])
    def config_script(self, request, pk=None):
        """Public endpoint for router to download configuration script"""
        
        # STEP 1: Start in PUBLIC schema to find tenant
        from django.db import connection
        connection.set_schema_to_public()
        
        from apps.core.models import Tenant, Domain
        from apps.network.models.router_models import Router
        
        # STEP 2: Find tenant by router ID (search across ALL tenants)
        tenants = Tenant.objects.filter(is_active=True)
        router = None
        
        for tenant in tenants:
            try:
                # Switch to THIS tenant's schema
                connection.set_tenant(tenant)
                
                # Try to find router in this tenant
                try:
                    router = Router.objects.filter(id=pk).first()
                    if router:
                        break  # Found it!
                except Exception:
                    continue  # Router table missing in this tenant, try next
                    
            except Exception as e:
                print(f"DEBUG: Error checking tenant {tenant.subdomain}: {e}")
                continue
        
        # STEP 3: If no router found, 404
        if not router:
            connection.set_schema_to_public()
            return Response({"error": "Router not found or access denied"}, status=404)
        
        # STEP 4: Verify auth_key
        auth_key = request.query_params.get('auth_key')
        if not auth_key or auth_key != router.auth_key:
            connection.set_schema_to_public()
            return Response({"error": "Invalid auth key"}, status=401)
        
        # STEP 5: Generate config (same as before)
        version = request.query_params.get('version', '7')
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

:local routerModel [/system routerboard get model];
:if ([:len $routerModel] = 0) do={{
    :set routerModel "Unknown";
}};

:put ("Router Model: $routerModel");

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
        # STEP 6: Switch back to public BEFORE response
        connection.set_schema_to_public()

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
            
            # IMPORTANT: We need to search across ALL tenant schemas
            # First, switch to public schema
            from django.db import connection
            connection.set_schema_to_public()
            
            # Get all tenants - IMPORT DIRECTLY FROM YOUR CORE MODELS
            from apps.core.models import Tenant  # Import from your app
            tenants = Tenant.objects.all()
            
            found_router = None
            found_tenant = None
            
            # Search through each tenant's schema
            for tenant in tenants:
                try:
                    # Switch to tenant schema
                    connection.set_tenant(tenant)
                    
                    # Try to find router in this tenant's schema
                    from apps.network.models.router_models import Router
                    router = Router.objects.filter(auth_key=auth_key).first()
                    
                    if router:
                        found_router = router
                        found_tenant = tenant
                        break
                except Exception as e:
                    logger.warning(f"Error searching in tenant {tenant.schema_name}: {str(e)}")
                    continue
            
            if not found_router:
                # Switch back to public schema
                connection.set_schema_to_public()
                return Response({"error": "Invalid authentication key"}, status=404)
            
            # Get IP address
            x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
            if x_forwarded_for:
                ip = x_forwarded_for.split(',')[0].strip()
            else:
                ip = request.META.get('REMOTE_ADDR', 'Unknown')
            
            # Update router (we're already in the correct tenant schema)
            found_router.ip_address = ip
            found_router.mac_address = data.get('mac', 'Unknown')
            found_router.firmware_version = data.get('version', 'Unknown')
            found_router.model = data.get('model', 'Unknown')
            found_router.is_authenticated = True
            found_router.authenticated_at = timezone.now()
            found_router.status = "online"
            found_router.last_seen = timezone.now()
            found_router.save()
            
            # Create event
            from apps.network.models.router_models import RouterEvent
            RouterEvent.objects.create(
                router=found_router,
                event_type="auth_success",
                message=f"Router authenticated from {ip}",
            )
            
            # Switch back to public schema for response
            connection.set_schema_to_public()
            
            return Response({
                "status": "success",
                "message": "Router authenticated successfully",
                "router_id": found_router.id,
                "router_name": found_router.name,
                "tenant": found_tenant.subdomain if found_tenant else None,
            })
            
        except Exception as e:
            logger.error(f"Router authentication error: {str(e)}")
            # Ensure we're back in public schema on error
            try:
                from django.db import connection
                connection.set_schema_to_public()
            except:
                pass
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
            
            # Switch to public schema
            from django.db import connection
            connection.set_schema_to_public()
            
            # Get all tenants - IMPORT DIRECTLY FROM YOUR CORE MODELS
            from apps.core.models import Tenant  # Import from your app
            tenants = Tenant.objects.all()
            
            found_router = None
            current_tenant = None
            
            # Search through each tenant's schema
            for tenant in tenants:
                try:
                    # Switch to tenant schema
                    connection.set_tenant(tenant)
                    
                    # Try to find router in this tenant's schema
                    from apps.network.models.router_models import Router
                    router = Router.objects.filter(auth_key=auth_key).first()
                    
                    if router:
                        found_router = router
                        current_tenant = tenant
                        break
                except Exception as e:
                    logger.warning(f"Error searching in tenant {tenant.schema_name}: {str(e)}")
                    continue
            
            if not found_router:
                # Switch back to public schema
                connection.set_schema_to_public()
                return Response({"error": "Invalid key"}, status=404)
            
            # Update heartbeat
            found_router.last_seen = timezone.now()
            found_router.status = 'online'
            
            # Optional: Update IP if provided
            if 'ip' in data:
                found_router.ip_address = data['ip']
                found_router.save(update_fields=['last_seen', 'status', 'ip_address'])
            else:
                found_router.save(update_fields=['last_seen', 'status'])
            
            logger.debug(f"Heartbeat from router {found_router.name} (ID: {found_router.id}) in tenant {current_tenant.schema_name}")
            
            # Switch back to public schema
            connection.set_schema_to_public()
            
            return Response({"status": "ok", "router_id": found_router.id})
            
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
            # Ensure we're back in public schema
            try:
                from django.db import connection
                connection.set_schema_to_public()
            except:
                pass
            return Response({"error": str(e)}, status=400)