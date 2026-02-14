# apps/network/views/router_views.py
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.db.models import Sum, Avg, F, Count
from django.http import HttpResponse, Http404
import textwrap  # <--- Add this
from apps.network.services.mikrotik_script_generator import MikrotikScriptGenerator
from rest_framework import serializers
import json
import logging
import socket
from apps.network.models.router_models import Router, RouterEvent
from apps.network.serializers.router_serializers import RouterSerializer, RouterEventSerializer
from apps.core.permissions import HasCompanyAccess
import apps.network.integrations.mikrotik_api as mikrotik_api_module
logger = logging.getLogger(__name__)

def find_router_across_tenants(router_id=None, auth_key=None, router_name=None):
    """
    Helper function to search for a router across all tenants.
    Returns (router, tenant) or (None, None) if not found.
    """
    from django.db import connection
    connection.set_schema_to_public()
    
    from apps.core.models import Tenant
    tenants = Tenant.objects.filter(is_active=True)
    
    found_router = None
    found_tenant = None
    
    for tenant in tenants:
        try:
            connection.set_tenant(tenant)
            try:
                if router_id:
                    found_router = Router.objects.filter(id=router_id).first()
                elif auth_key:
                    found_router = Router.objects.filter(auth_key=auth_key).first()
                elif router_name:
                    found_router = Router.objects.filter(name__icontains=router_name).first()
                    if not found_router:
                        found_router = Router.objects.filter(auth_key=router_name).first()
                
                if found_router:
                    found_tenant = tenant
                    break
            except Exception:
                continue
        except Exception:
            continue
    
    return found_router, found_tenant

# ────────────────────────────────────────────────────────────────
# CERTIFICATE DOWNLOAD VIEW 
# ────────────────────────────────────────────────────────────────
def download_router_cert(request, router_id, cert_type):
    """
    Serves the certificate file for a router just like LipaNet API.
    Used by the router's /tool fetch command.
    """
    # 1. Find Router (Handle Multi-tenancy)
    router, tenant = find_router_across_tenants(router_id=router_id)
    
    if not router:
        raise Http404("Router not found")

    # 2. Switch to Tenant Context
    from django.db import connection
    connection.set_tenant(tenant)

    try:
        # 3. Select Data
        content = ""
        filename = ""
        label = ""

        if cert_type == 'ca.crt':
            content = router.ca_certificate
            filename = "netily-ca.crt"
            label = "CERTIFICATE"
        elif cert_type == 'client.crt':
            content = router.client_certificate
            filename = "netily-client.crt"
            label = "CERTIFICATE"
        elif cert_type == 'client.key':
            content = router.client_key
            filename = "netily-client.key"
            label = "PRIVATE KEY"
        
        if not content:
            raise Http404(f"Certificate {cert_type} is empty")

        # 4. Clean & Format (Strict PEM for RouterOS)
        # Strip existing headers to get raw base64
        clean = content.replace(f'-----BEGIN {label}-----', '')
        clean = clean.replace(f'-----END {label}-----', '')
        # Remove whitespace/newlines
        clean = clean.replace(' ', '').replace('\r', '').replace('\n', '').replace('\t', '').strip()
        
        # Chunk into 64-char lines
        chunked = textwrap.fill(clean, 64)
        
        # Rebuild
        final_pem = f"-----BEGIN {label}-----\n{chunked}\n-----END {label}-----\n"
        
        # 5. Return File
        response = HttpResponse(final_pem, content_type='text/plain')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    finally:
        # 6. Safety: Switch back to public schema
        connection.set_schema_to_public()

class RouterViewSet(viewsets.ModelViewSet):
    serializer_class = RouterSerializer
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['router_type', 'status', 'is_active', 'config_type']
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
        router = serializer.save()
        
        # ── CLOUD CONTROLLER: Auto-provision VPN tunnel for new router ──
        try:
            from apps.vpn.services.vpn_provisioning_service import VPNProvisioningService
            
            vpn_service = VPNProvisioningService()
            vpn_service.provision_router(router)
            logger.info(f"VPN provisioned for new router: {router.name} (IP: {router.vpn_ip_address})")
        except Exception as e:
            # Don't fail router creation if VPN provisioning fails
            # Admin can re-provision later from the router detail page
            logger.error(f"VPN provisioning failed for router {router.name}: {e}", exc_info=True)
       
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
    # CONFIGURATION ENDPOINTS (UPDATED TO USE SINGLE GENERATOR)
    # ────────────────────────────────────────────────────────────────
    @action(detail=True, methods=['get'], url_path='one-liner', permission_classes=[AllowAny])
    def one_liner_script(self, request, pk=None):
        """Generate one-liner script"""
        router, tenant = find_router_across_tenants(router_id=pk)
        
        if not router:
            return Response({"error": "Router not found"}, status=404)
        
        # Switch to tenant schema to generate script
        from django.db import connection
        connection.set_tenant(tenant)
        
        # Generate one-liner script using single generator
        generator = MikrotikScriptGenerator(router)
        one_liner = generator.generate_one_liner()
        
        # Switch back to public
        connection.set_schema_to_public()
        
        response = HttpResponse(one_liner, content_type='text/plain')
        response['Content-Disposition'] = f'attachment; filename="netily-one-liner-{router.id}.txt"'
        return response
   
    @action(detail=True, methods=['get'], url_path='full-config', permission_classes=[AllowAny])
    def full_config_script(self, request, pk=None):
        """Full configuration script"""
        router, tenant = find_router_across_tenants(router_id=pk)
        
        if not router:
            return Response({"error": "Router not found"}, status=404)
        
        # Verify auth_key
        auth_key = request.query_params.get('auth_key')
        if not auth_key or auth_key != router.auth_key:
            return Response({"error": "Invalid auth key"}, status=401)
        
        # Switch to tenant schema
        from django.db import connection
        connection.set_tenant(tenant)
        
        # Generate configuration using single generator
        version = request.query_params.get('version', '7')
        config_type = request.query_params.get('type', router.config_type)
        
        generator = MikrotikScriptGenerator(router)
        script_content = generator.generate_full_script()
        
        # Log the configuration generation
        RouterEvent.objects.create(
            router=router,
            event_type='script_executed',
            message=f"Full configuration script generated for {config_type} setup",
            details={
                'version': version,
                'config_type': config_type,
            }
        )
        
        # Switch back to public
        connection.set_schema_to_public()
        
        response = HttpResponse(script_content, content_type='text/plain')
        response['Content-Disposition'] = f'attachment; filename="netily-full-config-{router.id}.rsc"'
        return response
   
    @action(detail=True, methods=['get'], url_path='debug-script', permission_classes=[AllowAny])
    def debug_script(self, request, pk=None):
        """Debug script endpoint to analyze script generation"""
        router, tenant = find_router_across_tenants(router_id=pk)
        
        if not router:
            return Response({"error": "Router not found"}, status=404)
        
        # Verify auth_key
        auth_key = request.query_params.get('auth_key')
        if not auth_key or auth_key != router.auth_key:
            return Response({"error": "Invalid auth key"}, status=401)
        
        # Switch to tenant schema
        from django.db import connection
        connection.set_tenant(tenant)
        
        # Assuming generate_debug_script is implemented; if not, implement or remove
        # For now, placeholder - adjust based on actual implementation
        generator = MikrotikScriptGenerator(router)
        try:
            script = generator.generate_full_script()
            response_data = {
                'full_script': script,
                # Add debug logic if needed, e.g., line analysis
            }
        except Exception as e:
            response_data = {'error': str(e)}
        
        # Switch back to public
        connection.set_schema_to_public()
        
        return Response(response_data)
   
    @action(detail=True, methods=['get'], url_path='lipa-style', permission_classes=[AllowAny])
    def lipa_style_script(self, request, pk=None):
        """Generate Lipa Net style configuration script"""
        router, tenant = find_router_across_tenants(router_id=pk)
        
        if not router:
            return Response({"error": "Router not found"}, status=404)
        
        # Verify auth_key
        auth_key = request.query_params.get('auth_key')
        if not auth_key or auth_key != router.auth_key:
            return Response({"error": "Invalid auth key"}, status=401)
        
        # Switch to tenant schema
        from django.db import connection
        connection.set_tenant(tenant)
        
        # Generate configuration using single generator
        version = request.query_params.get('version', '7')
        
        generator = MikrotikScriptGenerator(router)
        
        if request.query_params.get('type') == 'one_liner':
            script_content = generator.generate_one_liner()
        else:
            script_content = generator.generate_full_script()
        
        # Log the configuration generation
        RouterEvent.objects.create(
            router=router,
            event_type='script_executed',
            message=f"Lipa-style configuration script generated",
            details={
                'version': version,
            }
        )
        
        # Switch back to public
        connection.set_schema_to_public()
        
        response = HttpResponse(script_content, content_type='text/plain')
        response['Content-Disposition'] = f'attachment; filename="netily-config-{router.id}.rsc"'
        return response
   
    @action(detail=False, methods=['get'], url_path=r'download/script/(?P<version>\d+)/(?P<router_name>[^/]+)', permission_classes=[AllowAny])
    def download_script(self, request, version=None, router_name=None):
        """Download script endpoint"""
        router, tenant = find_router_across_tenants(router_name=router_name)
        
        if not router:
            return Response({"error": "Router not found"}, status=404)
        
        # Switch to tenant schema
        from django.db import connection
        connection.set_tenant(tenant)
        
        # Generate the one-liner script using single generator
        generator = MikrotikScriptGenerator(router)
        one_liner = generator.generate_one_liner()
        
        # Switch back to public
        connection.set_schema_to_public()
        
        response = HttpResponse(one_liner, content_type='text/plain')
        response['Content-Disposition'] = f'attachment; filename="netily-one-liner-{router.id}.txt"'
        return response
   
    @action(detail=True, methods=['get'], url_path='openvpn-config', permission_classes=[AllowAny])
    def openvpn_config(self, request, pk=None):
        """Generate OpenVPN configuration file"""
        router, tenant = find_router_across_tenants(router_id=pk)
        
        if not router:
            return Response({"error": "Router not found"}, status=404)
        
        # Verify auth_key
        auth_key = request.query_params.get('auth_key')
        if not auth_key or auth_key != router.auth_key:
            return Response({"error": "Invalid auth key"}, status=401)
        
        # Switch to tenant schema
        from django.db import connection
        connection.set_tenant(tenant)
        
        # ────────────────────────────────────────────────────────────
        # DYNAMIC CERTIFICATES — pulled from Router model fields
        # For user/pass VPN (v4 architecture) this .ovpn is primarily
        # for external troubleshooting / diagnostic connections.
        # ────────────────────────────────────────────────────────────
        ca_cert = (router.ca_certificate or '').strip()
        client_cert = (router.client_certificate or '').strip()
        client_key = (router.client_key or '').strip()

        if not ca_cert:
            connection.set_schema_to_public()
            return Response(
                {"error": "No CA certificate configured for this router. "
                 "Upload certificates in the router admin panel first."},
                status=400
            )

        # Build the .ovpn config — user/pass auth with optional certs
        openvpn_config = f"""# Netily OpenVPN Configuration
# Generated for {router.name} at {timezone.now()}
client
dev tun
proto udp
remote {router.openvpn_server} {router.openvpn_port}
resolv-retry infinite
nobind
persist-key
persist-tun
cipher AES-256-CBC
auth SHA256
auth-user-pass
verb 3
mute 20
<ca>
{ca_cert}
</ca>
"""
        # Only include client cert/key if they exist (cert-based auth)
        if client_cert:
            openvpn_config += f"""<cert>
{client_cert}
</cert>
"""
        if client_key:
            openvpn_config += f"""<key>
{client_key}
</key>
"""
        
        # Switch back to public
        connection.set_schema_to_public()
        
        response = HttpResponse(openvpn_config, content_type='application/x-openvpn-profile')
        response['Content-Disposition'] = f'attachment; filename="netily-{router.id}.ovpn"'
        return response
   
    @action(detail=True, methods=['get'], url_path='simple-config', permission_classes=[AllowAny])
    def simple_config_script(self, request, pk=None):
        """Simple configuration endpoint (backward compatible)"""
        return self.full_config_script(request, pk)
   
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def generate_config(self, request, pk=None):
        """Generate and preview configuration"""
        router = self.get_object()
        
        config_type = request.data.get('config_type', router.config_type)
        version = request.data.get('version', '7')
        
        # Use single generator
        generator = MikrotikScriptGenerator(router)
        config_script = generator.generate_full_script()
        
        return Response({
            'status': 'success',
            'router_id': router.id,
            'router_name': router.name,
            'config_type': config_type,
            'version': version,
            'preview': config_script[:500] + "..." if len(config_script) > 500 else config_script,
            'one_liner': generator.generate_one_liner(),
        })
   
    # ────────────────────────────────────────────────────────────────
    # ISP CONFIGURATION MANAGEMENT
    # ────────────────────────────────────────────────────────────────
   
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def update_config_settings(self, request, pk=None):
        """Update router configuration settings"""
        router = self.get_object()
        
        # Update basic settings - aligned with new model fields
        fields_to_update = [
            'config_type', 'gateway_cidr', 'dns_name', 'hotspot_interfaces',
            'wan_interface', 'enable_hotspot', 'enable_pppoe', 'pppoe_pool',
            'enable_openvpn', 'openvpn_server', 'openvpn_port',
            'radius_server', 'radius_port'
        ]
        
        updated_fields = []
        for field in fields_to_update:
            if field in request.data:
                setattr(router, field, request.data[field])
                updated_fields.append(field)
        
        if updated_fields:
            router.save()
            
            RouterEvent.objects.create(
                router=router,
                event_type='config_change',
                message=f"Router configuration updated: {', '.join(updated_fields)}",
                details={'updated_fields': updated_fields}
            )
        
        return Response({
            'status': 'success',
            'message': f'Updated {len(updated_fields)} fields',
            'updated_fields': updated_fields,
            'router': RouterSerializer(router).data
        })
   
    @action(detail=True, methods=['post'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def test_isp_config(self, request, pk=None):
        """Test ISP configuration by applying it to router"""
        router = self.get_object()
        
        if router.router_type != 'mikrotik':
            return Response({"error": "This action is only available for Mikrotik routers"}, status=400)
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
            
            # Test connection
            if not api.connect():
                return Response({"error": "Failed to connect to router"}, status=400)
            
            # Test basic commands
            test_results = []
            
            # Test system identity
            try:
                identity = api._execute('/system/identity')[0]
                test_results.append({
                    'test': 'system_identity',
                    'status': 'success',
                    'result': identity
                })
            except Exception as e:
                test_results.append({
                    'test': 'system_identity',
                    'status': 'failed',
                    'error': str(e)
                })
            
            # Test interface listing
            try:
                interfaces = api.get_interfaces()
                test_results.append({
                    'test': 'interfaces',
                    'status': 'success',
                    'result': f"Found {len(interfaces)} interfaces"
                })
            except Exception as e:
                test_results.append({
                    'test': 'interfaces',
                    'status': 'failed',
                    'error': str(e)
                })
            
            api.disconnect()
            
            RouterEvent.objects.create(
                router=router,
                event_type='config_sync',
                message="ISP configuration test completed",
                details={'test_results': test_results}
            )
            
            return Response({
                'status': 'success',
                'message': 'Configuration test completed',
                'test_results': test_results
            })
            
        except Exception as e:
            logger.error(f"Failed to test ISP config for router {router.name}: {str(e)}")
            return Response({"error": str(e)}, status=400)
   
    @action(detail=True, methods=['get'], permission_classes=[IsAuthenticated, HasCompanyAccess])
    def config_summary(self, request, pk=None):
        """Get configuration summary"""
        router = self.get_object()
        
        summary = {
            'router': {
                'name': router.name,
                'config_type': router.get_config_type_display(),
                'ip_address': router.ip_address,
                'status': router.status,
            },
            'network': {
                'gateway_cidr': router.gateway_cidr,
                'gateway_ip': router.gateway_ip,
                'pool_range': router.pool_range,
                'dns_name': router.dns_name,
                'pppoe_pool': router.pppoe_pool,
            },
            'services': {
                'hotspot_enabled': router.enable_hotspot,
                'pppoe_enabled': router.enable_pppoe,
                'openvpn_enabled': router.enable_openvpn,
                'openvpn_server': f"{router.openvpn_server}:{router.openvpn_port}",
            },
            'interfaces': {
                'wan': router.wan_interface,
                'hotspot_interfaces': router.hotspot_interfaces,
            },
            'authentication': {
                'is_authenticated': router.is_authenticated,
                'auth_key_exists': bool(router.auth_key),
                'shared_secret_exists': bool(router.shared_secret),
                'radius_server': f"{router.radius_server}:{router.radius_port}" if router.radius_server else 'Not configured',
            }
        }
        
        return Response(summary)
    
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
            # Configuration type stats
            "basic_routers": qs.filter(config_type='basic').count(),
            "hotspot_routers": qs.filter(config_type='hotspot').count(),
            "pppoe_routers": qs.filter(config_type='pppoe').count(),
            "isp_routers": qs.filter(config_type='isp').count(),
            "full_isp_routers": qs.filter(config_type='full_isp').count(),
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
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
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
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
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
        
        if not router.api_username or not router.api_password:
            return Response({"error": "API credentials not configured for this router"}, status=400)
        
        try:
            api = mikrotik_api_module.MikrotikAPI(router)
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
        router, tenant = find_router_across_tenants(router_id=pk)
        
        if not router:
            return Response({"error": "Router not found"}, status=404)
        
        # Switch to tenant schema
        from django.db import connection
        connection.set_tenant(tenant)
        
        # Generate simple script using single generator
        generator = MikrotikScriptGenerator(router)
        one_liner = generator.generate_one_liner()
        
        # Switch back to public
        connection.set_schema_to_public()
        
        response = HttpResponse(one_liner, content_type='text/plain')
        response['Content-Disposition'] = f'attachment; filename="netily-{router.id}.rsc"'
        return response
   
 # CHANGE: detail=False (Allows access without ID in URL)
    @action(detail=False, methods=['get'], url_path='config', permission_classes=[AllowAny])
    def config_script(self, request, pk=None):
        """Public endpoint for router to download configuration script"""
        # CHANGE: Get auth_key first
        auth_key = request.query_params.get('auth_key')
        if not auth_key:
            return Response({"error": "Auth key required"}, status=400)

        # CHANGE: Find router using the auth_key, not the pk
        router, tenant = find_router_across_tenants(auth_key=auth_key)
        
        if not router:
            return Response({"error": "Router not found or access denied"}, status=404)
        
        # Switch to tenant schema
        from django.db import connection
        connection.set_tenant(tenant)
        
        # Generate config using single generator
        generator = MikrotikScriptGenerator(router)
        script_content = generator.generate_full_script()
        
        # Switch back to public
        connection.set_schema_to_public()
        response = HttpResponse(script_content, content_type='text/plain')
        response['Content-Disposition'] = f'attachment; filename="netily-config-{router.id}.rsc"'
        return response
   
    @action(detail=True, methods=['get'], url_path='auth-key')
    def auth_key(self, request, pk=None):
        router = self.get_object()
        
        # Use single generator
        generator = MikrotikScriptGenerator(router)
        one_liner = generator.generate_one_liner()
        
        base_url = request.build_absolute_uri('/').rstrip('/')
        
        return Response({
            'auth_key': router.auth_key,
            'one_liner': one_liner,
            'config_types': dict(Router.CONFIG_TYPES),
            'current_config': router.config_type,
            'is_authenticated': router.is_authenticated,
            'authenticated_at': router.authenticated_at,
            'shared_secret': router.shared_secret,
            'config_endpoints': {
                'one_liner': f"{base_url}/api/v1/network/routers/{router.id}/one-liner/?auth_key={router.auth_key}",
                'full_config': f"{base_url}/api/v1/network/routers/{router.id}/full-config/?auth_key={router.auth_key}",
                'lipa_style': f"{base_url}/api/v1/network/routers/{router.id}/lipa-style/?auth_key={router.auth_key}",
                'simple_config': f"{base_url}/api/v1/network/routers/{router.id}/simple-config/?auth_key={router.auth_key}",
                'openvpn_config': f"{base_url}/api/v1/network/routers/{router.id}/openvpn-config/?auth_key={router.auth_key}",
                'debug_script': f"{base_url}/api/v1/network/routers/{router.id}/debug-script/?auth_key={router.auth_key}",
                'download_script': f"{base_url}/download/script/7/{router.auth_key}",
            }
        })

    # ------------------------------------------
    # CLOUD CONTROLLER / VPN ACTIONS
    # ------------------------------------------

    @action(detail=True, methods=['get'], url_path='vpn_status')
    def vpn_status(self, request, pk=None):
        """Get VPN tunnel status for a router."""
        router = self.get_object()
        
        tunnel_status = 'unknown'
        bytes_received = 0
        bytes_sent = 0
        connected_since = None
        certificate_expires_at = None
        
        # Try to get live tunnel info from OpenVPN management
        if router.vpn_provisioned and router.vpn_ip_address:
            try:
                from apps.vpn.services.openvpn_management import OpenVPNManagementClient
                mgmt = OpenVPNManagementClient()
                clients = mgmt.get_connected_clients()
                for client in clients:
                    if client.get('virtual_address') == router.vpn_ip_address:
                        tunnel_status = 'connected'
                        bytes_received = client.get('bytes_received', 0)
                        bytes_sent = client.get('bytes_sent', 0)
                        connected_since = client.get('connected_since')
                        break
                else:
                    tunnel_status = 'disconnected'
            except Exception as e:
                logger.warning(f"Could not check VPN status for router {router.id}: {e}")
                tunnel_status = 'unknown'
        
        # Get certificate expiry
        if router.vpn_certificate_id:
            try:
                cert = router.vpn_certificate
                if cert and cert.expires_at:
                    certificate_expires_at = cert.expires_at.isoformat()
            except Exception:
                pass
        
        return Response({
            'vpn_provisioned': router.vpn_provisioned,
            'vpn_ip_address': router.vpn_ip_address,
            'vpn_provisioned_at': router.vpn_provisioned_at.isoformat() if router.vpn_provisioned_at else None,
            'tunnel_status': tunnel_status,
            'last_seen': router.vpn_last_seen.isoformat() if hasattr(router, 'vpn_last_seen') and router.vpn_last_seen else None,
            'bytes_received': bytes_received,
            'bytes_sent': bytes_sent,
            'connected_since': connected_since,
            'certificate_expires_at': certificate_expires_at,
        })
    
    @action(detail=True, methods=['post'], url_path='reprovision_vpn')
    def reprovision_vpn(self, request, pk=None):
        """(Re-)provision VPN certificates and CCD for a router."""
        router = self.get_object()
        
        try:
            from apps.vpn.services.vpn_provisioning_service import VPNProvisioningService
            service = VPNProvisioningService()
            result = service.provision_router(router)
            
            router.refresh_from_db()
            
            RouterEvent.objects.create(
                router=router,
                event_type='vpn_provisioned',
                message=f"VPN {'re-' if result.get('reprovisioned') else ''}provisioned — IP: {router.vpn_ip_address}"
            )
            
            return Response({
                'status': 'success',
                'vpn_ip': router.vpn_ip_address,
            })
        except Exception as e:
            logger.error(f"VPN provisioning failed for router {router.id}: {e}")
            return Response({'error': str(e)}, status=400)
    
    @action(detail=True, methods=['post'], url_path='revoke_vpn')
    def revoke_vpn(self, request, pk=None):
        """Revoke VPN access for a router — removes CCD, marks certificate revoked."""
        router = self.get_object()
        
        try:
            # Remove CCD file
            if router.vpn_ip_address:
                from apps.vpn.services.ccd_manager import CCDManager
                ccd = CCDManager()
                ccd.remove_client(f"router-{router.id}")
            
            # Clear VPN fields
            router.vpn_provisioned = False
            router.vpn_ip_address = None
            router.ca_certificate = ''
            router.client_certificate = ''
            router.client_key = ''
            router.save(update_fields=[
                'vpn_provisioned', 'vpn_ip_address',
                'ca_certificate', 'client_certificate', 'client_key'
            ])
            
            RouterEvent.objects.create(
                router=router,
                event_type='vpn_revoked',
                message="VPN access revoked"
            )
            
            return Response({'status': 'success'})
        except Exception as e:
            logger.error(f"VPN revocation failed for router {router.id}: {e}")
            return Response({'error': str(e)}, status=400)

# ────────────────────────────────────────────────────────────────
# ROUTER PORTS & HOTSPOT CONFIGURATION VIEWS
# ────────────────────────────────────────────────────────────────
class RouterPortsView(APIView):
    """
    GET /api/v1/network/routers/{id}/ports/
   
    List all ethernet/wireless/bridge interfaces on the router with their current usage.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
   
    def get(self, request, pk):
        try:
            router = Router.objects.get(pk=pk)
        except Router.DoesNotExist:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
       
        # Check if router is reachable
        if router.status != 'online':
            return Response({
                'error': 'Router is offline',
                'message': 'Cannot retrieve ports from an offline router'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
       
        try:
            mikrotik = mikrotik_api_module.MikrotikAPI(router)
            ports = mikrotik.get_ports_with_usage()
           
            return Response({
                'router_id': router.id,
                'router_name': router.name,
                'ports': ports,
            })
       
        except Exception as e:
            logger.error(f"Failed to get ports for router {pk}: {e}")
            return Response({
                'error': 'Failed to retrieve ports',
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class RouterHotspotConfigView(APIView):
    """
    GET /api/v1/network/routers/{id}/hotspot/config/
   
    Get current hotspot configuration from the router.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
   
    def get(self, request, pk):
        try:
            router = Router.objects.get(pk=pk)
        except Router.DoesNotExist:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
       
        # Check if router is reachable
        if router.status != 'online':
            return Response({
                'error': 'Router is offline',
                'message': 'Cannot retrieve hotspot config from an offline router'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
       
        try:
            mikrotik = mikrotik_api_module.MikrotikAPI(router)
            config = mikrotik.get_hotspot_config()
           
            # Add router info
            config['router_id'] = router.id
            config['router_name'] = router.name
           
            return Response(config)
       
        except Exception as e:
            logger.error(f"Failed to get hotspot config for router {pk}: {e}")
            return Response({
                'error': 'Failed to retrieve hotspot configuration',
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class RouterHotspotConfigureView(APIView):
    """
    POST /api/v1/network/routers/{id}/hotspot/configure/
   
    Configure hotspot on the router.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
   
    def post(self, request, pk):
        try:
            router = Router.objects.get(pk=pk)
        except Router.DoesNotExist:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
       
        # Check if router is reachable
        if router.status != 'online':
            return Response({
                'error': 'Router is offline',
                'message': 'Cannot configure hotspot on an offline router'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
       
        # Validate required fields
        config = request.data
       
        if not config.get('interface'):
            return Response({
                'error': 'Interface is required'
            }, status=status.HTTP_400_BAD_REQUEST)
       
        if not config.get('network', {}).get('network_address'):
            return Response({
                'error': 'Network address is required'
            }, status=status.HTTP_400_BAD_REQUEST)
       
        if not config.get('network', {}).get('pool_range'):
            return Response({
                'error': 'Pool range is required'
            }, status=status.HTTP_400_BAD_REQUEST)
       
        try:
            mikrotik = mikrotik_api_module.MikrotikAPI(router)
            result = mikrotik.configure_hotspot(config)
           
            if result.get('success'):
                # Log the configuration event
                RouterEvent.objects.create(
                    router=router,
                    event_type='config_change',
                    message=f"Hotspot configured on interface {config.get('interface')}",
                    details={
                        'interface': config.get('interface'),
                        'server_name': result.get('server_name'),
                        'network': config.get('network'),
                        'configured_by': request.user.email,
                    }
                )
               
                # Update router config_type if needed
                if router.config_type != 'hotspot':
                    router.config_type = 'hotspot'
                    router.save(update_fields=['config_type'])
               
                return Response({
                    'success': True,
                    'message': 'Hotspot configured successfully',
                    'result': result,
                })
            else:
                return Response({
                    'success': False,
                    'message': 'Hotspot configuration failed',
                    'result': result,
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
       
        except Exception as e:
            logger.error(f"Failed to configure hotspot for router {pk}: {e}")
            return Response({
                'error': 'Failed to configure hotspot',
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class RouterHotspotDisableView(APIView):
    """
    POST /api/v1/network/routers/{id}/hotspot/disable/
   
    Disable hotspot server on the router.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
   
    def post(self, request, pk):
        try:
            router = Router.objects.get(pk=pk)
        except Router.DoesNotExist:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
       
        # Check if router is reachable
        if router.status != 'online':
            return Response({
                'error': 'Router is offline',
                'message': 'Cannot disable hotspot on an offline router'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
       
        server_name = request.data.get('server_name')  # Optional
       
        try:
            mikrotik = mikrotik_api_module.MikrotikAPI(router)
            result = mikrotik.disable_hotspot(server_name)
           
            if result:
                # Log the event
                RouterEvent.objects.create(
                    router=router,
                    event_type='config_change',
                    message=f"Hotspot disabled{' (' + server_name + ')' if server_name else ''}",
                    details={
                        'server_name': server_name,
                        'disabled_by': request.user.email,
                    }
                )
               
                return Response({
                    'success': True,
                    'message': 'Hotspot disabled',
                })
            else:
                return Response({
                    'success': False,
                    'message': 'Failed to disable hotspot — server not found',
                }, status=status.HTTP_404_NOT_FOUND)
       
        except Exception as e:
            logger.error(f"Failed to disable hotspot for router {pk}: {e}")
            return Response({
                'error': 'Failed to disable hotspot',
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RouterHotspotEnableView(APIView):
    """
    POST /api/v1/network/routers/{id}/hotspot/enable/
    
    Re-enable a previously disabled hotspot server on the router.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def post(self, request, pk):
        try:
            router = Router.objects.get(pk=pk)
        except Router.DoesNotExist:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        
        if router.status != 'online':
            return Response({
                'error': 'Router is offline',
                'message': 'Cannot enable hotspot on an offline router'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        
        server_name = request.data.get('server_name', 'netily-hotspot')
        
        try:
            mikrotik = mikrotik_api_module.MikrotikAPI(router)
            result = mikrotik.enable_hotspot(server_name)
            
            if result:
                RouterEvent.objects.create(
                    router=router,
                    event_type='config_change',
                    message=f"Hotspot enabled ({server_name})",
                    details={
                        'server_name': server_name,
                        'enabled_by': request.user.email,
                    }
                )
                return Response({
                    'success': True,
                    'message': f'Hotspot {server_name} enabled',
                })
            else:
                return Response({
                    'success': False,
                    'message': 'Failed to enable hotspot — server not found',
                }, status=status.HTTP_404_NOT_FOUND)
        
        except Exception as e:
            logger.error(f"Failed to enable hotspot for router {pk}: {e}")
            return Response({
                'error': 'Failed to enable hotspot',
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RouterBridgePortView(APIView):
    """
    POST /api/v1/network/routers/{id}/bridge/add-port/
    POST /api/v1/network/routers/{id}/bridge/remove-port/
    
    Assign or remove a physical interface to/from the hotspot bridge.
    This is the LipaNet "post-connection" workflow — once the VPN tunnel
    is up, the admin assigns ports from the dashboard.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def post(self, request, pk):
        try:
            router = Router.objects.get(pk=pk)
        except Router.DoesNotExist:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        
        if router.status != 'online':
            return Response({
                'error': 'Router is offline',
                'message': 'Cannot manage bridge ports on an offline router'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        
        interface_name = request.data.get('interface')
        action = request.data.get('action', 'add')  # 'add' or 'remove'
        bridge_name = request.data.get('bridge', 'netily-bridge')
        
        if not interface_name:
            return Response({
                'error': 'interface is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            mikrotik = mikrotik_api_module.MikrotikAPI(router)
            
            if action == 'remove':
                result = mikrotik.remove_port_from_bridge(interface_name)
                verb = 'removed from'
            else:
                result = mikrotik.add_port_to_bridge(interface_name, bridge_name)
                verb = 'added to'
            
            if result:
                # Update the hotspot_interfaces field on the Router model
                current_interfaces = set(router.hotspot_interfaces or [])
                if action == 'remove':
                    current_interfaces.discard(interface_name)
                else:
                    current_interfaces.add(interface_name)
                router.hotspot_interfaces = sorted(current_interfaces)
                router.save(update_fields=['hotspot_interfaces'])
                
                RouterEvent.objects.create(
                    router=router,
                    event_type='config_change',
                    message=f"Port {interface_name} {verb} {bridge_name}",
                    details={
                        'interface': interface_name,
                        'bridge': bridge_name,
                        'action': action,
                        'changed_by': request.user.email,
                    }
                )
                return Response({
                    'success': True,
                    'message': f'{interface_name} {verb} {bridge_name}',
                    'hotspot_interfaces': router.hotspot_interfaces,
                })
            else:
                return Response({
                    'success': False,
                    'message': f'Failed to {action} {interface_name}',
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        except Exception as e:
            logger.error(f"Bridge port operation failed for router {pk}: {e}")
            return Response({
                'error': f'Failed to {action} port',
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RouterHotspotUpdateView(APIView):
    """
    PATCH /api/v1/network/routers/{id}/hotspot/update/
    
    Update hotspot DNS name and/or IP pool range on a live router.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def patch(self, request, pk):
        try:
            router = Router.objects.get(pk=pk)
        except Router.DoesNotExist:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        
        if router.status != 'online':
            return Response({
                'error': 'Router is offline',
                'message': 'Cannot update hotspot on an offline router'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        
        dns_name = request.data.get('dns_name')
        pool_range = request.data.get('pool_range')
        
        if not dns_name and not pool_range:
            return Response({
                'error': 'Provide at least one of: dns_name, pool_range'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        config_data = {}
        if dns_name:
            config_data['dns_name'] = dns_name
        if pool_range:
            config_data['pool_range'] = pool_range
        
        try:
            mikrotik = mikrotik_api_module.MikrotikAPI(router)
            result = mikrotik.configure_hotspot(config_data)
            
            if result.get('success'):
                # Update the Router model to keep in sync
                if dns_name:
                    router.dns_name = dns_name
                    router.save(update_fields=['dns_name'])
                
                RouterEvent.objects.create(
                    router=router,
                    event_type='config_change',
                    message=f"Hotspot settings updated: {', '.join(config_data.keys())}",
                    details={
                        **config_data,
                        'updated_by': request.user.email,
                    }
                )
                return Response({
                    'success': True,
                    'message': 'Hotspot settings updated',
                    'updated': config_data,
                })
            else:
                return Response({
                    'success': False,
                    'message': 'Failed to update hotspot settings',
                    'error': result.get('error'),
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        except Exception as e:
            logger.error(f"Failed to update hotspot for router {pk}: {e}")
            return Response({
                'error': 'Failed to update hotspot settings',
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


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
            
            # Use helper to find router
            router, tenant = find_router_across_tenants(auth_key=auth_key)
            
            if not router:
                return Response({"error": "Invalid authentication key"}, status=404)
            
            # Switch to tenant schema
            from django.db import connection
            connection.set_tenant(tenant)
            
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
                details={
                    'ip': ip,
                    'mac': data.get('mac'),
                    'model': data.get('model'),
                    'version': data.get('version'),
                }
            )
            
            # Switch back to public schema
            connection.set_schema_to_public()
            
            return Response({
                "status": "success",
                "message": "Router authenticated successfully",
                "router_id": router.id,
                "router_name": router.name,
                "tenant": tenant.subdomain if tenant else None,
                "config_endpoints": {
                    "one_liner": f"/api/v1/network/routers/{router.id}/one-liner/?auth_key={auth_key}",
                    "full_config": f"/api/v1/network/routers/{router.id}/full-config/?auth_key={auth_key}",
                    "lipa_style": f"/api/v1/network/routers/{router.id}/lipa-style/?auth_key={auth_key}",
                }
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
            
            # Use helper to find router
            router, tenant = find_router_across_tenants(auth_key=auth_key)
            
            if not router:
                return Response({"error": "Invalid key"}, status=404)
            
            # Switch to tenant schema
            from django.db import connection
            connection.set_tenant(tenant)
            
            # Update heartbeat
            router.last_seen = timezone.now()
            router.status = 'online'
            
            # Optional: Update statistics if provided
            if 'active_users' in data:
                router.active_users = data['active_users']
            
            if 'total_users' in data:
                router.total_users = data['total_users']
            
            if 'uptime' in data:
                router.uptime = data['uptime']
            
            if 'ip' in data:
                router.ip_address = data['ip']
                router.save(update_fields=['last_seen', 'status', 'ip_address', 'active_users', 'total_users', 'uptime'])
            else:
                router.save(update_fields=['last_seen', 'status', 'active_users', 'total_users', 'uptime'])
            
            logger.debug(f"Heartbeat from router {router.name} (ID: {router.id}) in tenant {tenant.schema_name}")
            
            # Switch back to public
            connection.set_schema_to_public()
            
            return Response({
                "status": "ok",
                "router_id": router.id,
                "timestamp": timezone.now().isoformat()
            })
            
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
            try:
                from django.db import connection
                connection.set_schema_to_public()
            except:
                pass
            return Response({"error": str(e)}, status=400)