# apps/network/views/router_views.py
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from django.db.models import Sum, Avg, F, Count
from django.db import ProgrammingError
from django.http import HttpResponse, Http404
import textwrap
from apps.network.services.mikrotik_script_generator import MikrotikScriptGenerator
from rest_framework import serializers
import json
import logging
import socket
from apps.network.models.router_models import Router, RouterEvent
from apps.network.serializers.router_serializers import RouterSerializer, RouterEventSerializer
from apps.network.services.mikrotik_bridge_sync import sync_bridge_ports_to_router
from apps.core.permissions import HasCompanyAccess
import apps.network.integrations.mikrotik_api as mikrotik_api_module
logger = logging.getLogger(__name__)

def find_router_across_tenants(router_id=None, auth_key=None, router_name=None):
    """
    Fast tenant resolver using public RouterTenantIndex.
    Now supports both ID and Auth Key for O(1) speed.
    """
    from django.db import connection
    from django_tenants.utils import schema_context
    from apps.core.models import Tenant, RouterTenantIndex

    # 1) Try O(1) indexed lookup - supports both ID and auth_key
    index_row = None
    try:
        with schema_context('public'):
            # Check index by ID if provided
            if router_id:
                index_row = RouterTenantIndex.objects.select_related('tenant').filter(
                    router_id=router_id, is_active=True
                ).first()
            # Otherwise check by auth_key
            elif auth_key:
                index_row = RouterTenantIndex.objects.select_related('tenant').filter(
                    router_auth_key=auth_key, is_active=True
                ).first()
    except Exception:
        index_row = None

    if index_row:
        tenant = index_row.tenant
        try:
            connection.set_tenant(tenant)
            # Find the actual router object inside the tenant
            if router_id:
                router = Router.objects.filter(id=router_id).first()
            elif auth_key:
                router = Router.objects.filter(auth_key=auth_key).first()
            elif router_name:
                router = Router.objects.filter(name__icontains=router_name).first()
            else:
                router = None
            return router, tenant
        except Exception:
            pass

    # 2) Fallback legacy scan (kept for safety)
    connection.set_schema_to_public()
    tenants = Tenant.objects.filter(is_active=True)
    for tenant in tenants:
        try:
            connection.set_tenant(tenant)
            if router_id:
                found = Router.objects.filter(id=router_id).first()
            elif auth_key:
                found = Router.objects.filter(auth_key=auth_key).first()
            elif router_name:
                found = Router.objects.filter(name__icontains=router_name).first() or Router.objects.filter(auth_key=router_name).first()
            else:
                found = None

            if found:
                return found, tenant
        except Exception:
            continue

    return None, None

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

    def get_object(self):
        """
        Override DRF's default get_object to use our cross-tenant finder.
        This guarantees that all @action endpoints (live_status, events, etc.)
        will successfully find the router without strict queryset mismatches.
        """
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        pk = self.kwargs.get(lookup_url_kwarg)

        from django.db import connection as db_conn
        
        # 1. Use our custom cross-tenant finder
        router, tenant = find_router_across_tenants(router_id=pk)
        
        if not router:
            from django.http import Http404
            raise Http404("Router not found")
            
        # 2. Lock into the correct tenant schema
        db_conn.set_tenant(tenant)
        self.request.tenant = tenant
        
        # 3. Verify permissions before allowing the action
        self.check_object_permissions(self.request, router)
        
        return router

    def _ensure_tenant_context(self):
        """
        If the middleware hasn't set a tenant (e.g. request came via plain
        localhost), try to resolve it from the authenticated user's company.
        This lets the standard get_queryset() work without cross-tenant scans.
        """
        from django.db import connection as db_conn

        if hasattr(self.request, 'tenant') and self.request.tenant:
            return  # Already resolved by middleware

        user = self.request.user
        if not user or not user.is_authenticated:
            return

        try:
            company = getattr(user, 'company', None)
            if company:
                tenant = getattr(company, 'tenant', None)
                if tenant:
                    db_conn.set_tenant(tenant)
                    self.request.tenant = tenant
                    self.request.company = company
                    return

            # Superuser without company — pick the first active tenant
            if user.is_superuser:
                from apps.core.models import Tenant
                db_conn.set_schema_to_public()
                first_tenant = Tenant.objects.filter(is_active=True).exclude(
                    schema_name='public'
                ).first()
                if first_tenant:
                    db_conn.set_tenant(first_tenant)
                    self.request.tenant = first_tenant
        except Exception:
            pass

    def get_queryset(self):
        self._ensure_tenant_context()
       
        # All users in a tenant see only their tenant's routers
        qs = Router.objects.all()
       
        # Filter by tenant_subdomain if available
        if hasattr(self.request, 'tenant') and self.request.tenant:
            qs = qs.filter(tenant_subdomain=self.request.tenant.subdomain)
       
        return qs

    def retrieve(self, request, *args, **kwargs):
        """
        GET /routers/{pk}/  — Instant read. Can force refresh with ?refresh=true
        """
        from django.db import connection as db_conn

        pk = kwargs.get('pk') or args[0]
        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        
        db_conn.set_tenant(tenant)
        request.tenant = tenant
        
        # Optional manual refresh for admins
        refresh = request.query_params.get('refresh', 'false').lower() == 'true'
        if refresh and (request.user.is_staff or request.user.is_superuser):
            try:
                router.sync_status()
            except Exception as e:
                logger.warning(f"Refresh failed for router {router.id}: {e}")

        serializer = self.get_serializer(router)
        data = serializer.data
        data['status_age_seconds'] = (
            int((timezone.now() - router.last_seen).total_seconds())
            if router.last_seen else None
        )
        return Response(data)

    def list(self, request, *args, **kwargs):
        """
        GET /routers/  — Returns cached DB state instantly. No more live pings.
        """
        from django.db import connection as db_conn
        logger.info(f"[RouterViewSet.list] tenant={getattr(request, 'tenant', None)}")

        if hasattr(request, 'tenant') and request.tenant:
            queryset = self.filter_queryset(self.get_queryset())
            page = self.paginate_queryset(queryset)
            
            if page is not None:
                serializer = self.get_serializer(page, many=True)
                return self.get_paginated_response(serializer.data)

            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data)

        if not request.user.is_superuser:
            return Response({'count': 0, 'results': []})

        # Superuser: aggregate routers across all tenants instantly
        try:
            from apps.core.models import Tenant
            db_conn.set_schema_to_public()
            tenants = list(Tenant.objects.filter(is_active=True).exclude(schema_name='public'))
            all_routers = []
            for tenant in tenants:
                try:
                    db_conn.set_tenant(tenant)
                    for router in Router.objects.all():
                        data = RouterSerializer(router).data
                        all_routers.append(data)
                except Exception:
                    continue

            return Response({
                'count': len(all_routers),
                'results': all_routers,
            })
        except Exception as e:
            logger.error(f"Failed to list routers across tenants: {e}", exc_info=True)
            return Response({'count': 0, 'results': []})
    
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
        try:
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
        except ProgrammingError:
            # Tenant schema not fully migrated — return empty stats gracefully
            logger.warning("dashboard_stats: network_router table missing for current tenant")
            return Response({
                "total_routers": 0, "online_routers": 0, "offline_routers": 0,
                "warning_routers": 0, "maintenance_routers": 0,
                "total_connected_users": 0, "average_uptime": 0,
                "basic_routers": 0, "hotspot_routers": 0, "pppoe_routers": 0,
                "isp_routers": 0, "full_isp_routers": 0,
                "authenticated_routers": 0, "pending_authentication": 0,
            })
    
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
    # HOTSPOT CONFIGURATION ENDPOINT WITH SYNC_STATUS
    # ────────────────────────────────────────────────────────────────
    @action(detail=True, methods=['get', 'post'], url_path='hotspot/config')
    def hotspot_config(self, request, pk=None):
        """
        Get or update hotspot configuration.
        GET: Retrieve current hotspot config from the router
        POST: Update hotspot configuration on the router
        """
        router = self.get_object()
        
        # 🔥 NEW: Trigger a fresh sync so if they just plugged it in, it works immediately
        try:
            router.sync_status()
        except Exception as e:
            logger.warning(f"Failed to sync status for router {router.id} in hotspot_config: {e}")

        if router.status != 'online':
            return Response({
                "error": "Router is offline",
                "message": "Cannot access hotspot configuration. Please check your VPN tunnel and ensure the router is online."
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        if request.method == 'GET':
            # GET logic - retrieve hotspot config
            try:
                api = mikrotik_api_module.MikrotikAPI(router)
                config = api.get_hotspot_config()
                return Response(config)
            except Exception as e:
                logger.error(f"Failed to get hotspot config for router {router.id}: {e}")
                return Response({
                    "error": "Failed to retrieve hotspot configuration",
                    "message": str(e)
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        elif request.method == 'POST':
            # POST logic - update hotspot config
            try:
                api = mikrotik_api_module.MikrotikAPI(router)
                result = api.configure_hotspot(request.data)
                
                if result.get('success'):
                    RouterEvent.objects.create(
                        router=router,
                        event_type='config_change',
                        message="Hotspot configuration updated",
                        details={'updated_by': request.user.email}
                    )
                    return Response({'success': True, 'message': 'Hotspot configuration updated'})
                else:
                    return Response({
                        'success': False,
                        'message': 'Failed to update hotspot config',
                        'error': result.get('error')
                    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            except Exception as e:
                logger.error(f"Failed to update hotspot config for router {router.id}: {e}")
                return Response({
                    'error': 'Failed to update hotspot configuration',
                    'message': str(e)
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# ────────────────────────────────────────────────────────────────
# ROUTER PORT SCAN, PORTS & HOTSPOT CONFIGURATION VIEWS
# ────────────────────────────────────────────────────────────────

class RouterPortScanView(APIView):
    """
    GET /api/v1/network/routers/{id}/scan/

    TCP port scan against the router IP to verify which management
    services are reachable before attempting configuration.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]

    def get(self, request, pk):
        from django.db import connection
        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        connection.set_tenant(tenant)

        try:
            mikrotik = mikrotik_api_module.MikrotikAPI(router)
            result = mikrotik.scan_ports()

            return Response({
                'router_id': router.id,
                'router_name': router.name,
                'router_status': router.status,
                **result,
            })
        except Exception as e:
            logger.error(f"Port scan failed for router {pk}: {e}")
            return Response({
                'error': 'Port scan failed',
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class RouterPortsView(APIView):
    """
    GET /api/v1/network/routers/{id}/ports/
   
    List all ethernet/wireless/bridge interfaces on the router with their current usage.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
   
    def get(self, request, pk):
        from django.db import connection
        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        connection.set_tenant(tenant)
       
        # Check if router is reachable
        if router.status != 'online':
            return Response({
                'error': 'Router is offline',
                'message': 'Cannot retrieve ports from an offline router'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
       
        try:
            mikrotik = mikrotik_api_module.MikrotikAPI(router)
            ports = mikrotik.get_full_interface_detail()
           
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
        from django.db import connection
        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        connection.set_tenant(tenant)
       
        # Check if router is reachable
        if router.status != 'online':
            return Response({
                'error': 'Router is offline',
                'message': 'Cannot retrieve hotspot config from an offline router'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
       
        try:
            mikrotik = mikrotik_api_module.MikrotikAPI(router)
            raw = mikrotik.get_hotspot_config()
           
            servers = raw.get('servers', [])
            profiles = raw.get('profiles', [])
            is_configured = len(servers) > 0

            # Build structured response the frontend expects
            server_data = None
            profile_data = None
            if servers:
                srv = servers[0]  # primary server
                server_data = {
                    'name': srv.get('name', ''),
                    'interface': srv.get('interface', ''),
                    'address_pool': srv.get('address-pool', ''),
                    'profile': srv.get('profile', ''),
                    'idle_timeout': srv.get('idle-timeout', ''),
                    'keepalive_timeout': srv.get('keepalive-timeout', ''),
                    'login_by': srv.get('login-by', '').split(',') if srv.get('login-by') else [],
                    'disabled': srv.get('disabled', 'false') == 'true' if isinstance(srv.get('disabled'), str) else bool(srv.get('disabled', False)),
                    'addresses_per_mac': srv.get('addresses-per-mac', 2),
                }
            if profiles:
                prof = profiles[0]
                profile_data = {
                    'name': prof.get('name', ''),
                    'rate_limit': prof.get('rate-limit', ''),
                    'session_timeout': prof.get('session-timeout', ''),
                    'shared_users': prof.get('shared-users', 1),
                    'login_by': prof.get('login-by', ''),
                    'dns_name': prof.get('dns-name', ''),
                    'html_directory': prof.get('html-directory', ''),
                }

            # Try to get active sessions count
            active_sessions = 0
            try:
                mikrotik2 = mikrotik_api_module.MikrotikAPI(router)
                users = mikrotik2.get_active_hotspot_users()
                active_sessions = len(users) if users else 0
            except Exception:
                pass

            return Response({
                'router_id': router.id,
                'router_name': router.name,
                'is_configured': is_configured,
                'server': server_data,
                'profile': profile_data,
                'plans': [],  # Plans are managed in Django DB
                'active_sessions': active_sessions,
                'portal_url': f"http://{profile_data.get('dns_name', '')}" if profile_data and profile_data.get('dns_name') else None,
                'total_servers': len(servers),
                'total_profiles': len(profiles),
            })
       
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
        from django.db import connection
        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        connection.set_tenant(tenant)
       
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

            # Build flat config dict for full_hotspot_setup
            network = config.get('network', {})
            server = config.get('server', {})
            branding = config.get('branding', {})

            # FIX: Use safe defaults that don't prematurely disconnect users
            # idle_timeout defaults to 'none' (no idle disconnect)
            # keepalive_timeout defaults to '10m' (10 minutes, tolerant of network hiccups)
            setup_config = {
                'interface':         config['interface'],
                'gateway':           network.get('network_address', '10.5.50.1'),
                'network_mask':      network.get('network_mask', '24'),
                'pool_name':         network.get('pool_name', 'hs-pool-1'),
                'pool_range':        network.get('pool_range', '10.5.50.10-10.5.50.254'),
                'dns_server':        network.get('dns_server', '8.8.8.8'),
                'server_name':       server.get('name', 'hotspot1'),
                'idle_timeout':      server.get('idle_timeout', 'none'),
                'keepalive_timeout': server.get('keepalive_timeout', '10m'),
                'login_by':          ','.join(server.get('login_by', ['mac', 'http-chap'])),
                'dns_name':          branding.get('dns_name', ''),
            }

            result = mikrotik.full_hotspot_setup(setup_config)
           
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
                        'steps': result.get('steps', []),
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
                    'message': result.get('error', 'Hotspot configuration failed'),
                    'steps': result.get('steps', []),
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
        from django.db import connection
        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        connection.set_tenant(tenant)
       
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

            if result:  # disable_hotspot returns bool
                RouterEvent.objects.create(
                    router=router,
                    event_type='config_change',
                    message=f"Hotspot disabled{' (' + server_name + ')' if server_name else ''}",
                    details={
                        'server_name': server_name,
                        'disabled_by': request.user.email,
                    }
                )
                return Response({'success': True, 'message': 'Hotspot disabled'})
            else:
                return Response({
                    'success': False,
                    'message': 'Failed to disable hotspot',
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        except Exception as e:
            logger.error(f"Failed to disable hotspot for router {pk}: {e}")
            return Response({
                'error': 'Failed to disable hotspot',
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RouterHotspotEnableView(APIView):
    """
    POST /api/v1/network/routers/{id}/hotspot/enable/

    Enable hotspot server on the router.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]

    def post(self, request, pk):
        from django.db import connection
        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        connection.set_tenant(tenant)

        if router.status != 'online':
            return Response({
                'error': 'Router is offline',
                'message': 'Cannot enable hotspot on an offline router'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        server_name = request.data.get('server_name')  # Optional

        try:
            mikrotik = mikrotik_api_module.MikrotikAPI(router)
            result = mikrotik.enable_hotspot(server_name) if server_name else mikrotik.enable_hotspot()

            if result:  # enable_hotspot returns bool
                RouterEvent.objects.create(
                    router=router,
                    event_type='config_change',
                    message=f"Hotspot enabled{' (' + server_name + ')' if server_name else ''}",
                    details={
                        'server_name': server_name,
                        'enabled_by': request.user.email,
                    }
                )
                return Response({'success': True, 'message': 'Hotspot enabled'})
            else:
                return Response({
                    'success': False,
                    'message': 'Failed to enable hotspot',
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        except Exception as e:
            logger.error(f"Failed to enable hotspot for router {pk}: {e}")
            return Response({
                'error': 'Failed to enable hotspot',
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RouterHotspotUpdateView(APIView):
    """
    POST /api/v1/network/routers/{id}/hotspot/update/

    Update hotspot configuration (DNS name, IP pool range).
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]

    def post(self, request, pk):
        from django.db import connection
        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        connection.set_tenant(tenant)

        if router.status != 'online':
            return Response({
                'error': 'Router is offline',
                'message': 'Cannot update hotspot on an offline router'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        config_data = {}
        if request.data.get('dns_name'):
            config_data['dns_name'] = request.data['dns_name']
        if request.data.get('pool_range'):
            config_data['pool_range'] = request.data['pool_range']

        if not config_data:
            return Response({
                'error': 'No update data provided',
                'message': 'Provide dns_name and/or pool_range'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            mikrotik = mikrotik_api_module.MikrotikAPI(router)
            result = mikrotik.configure_hotspot(config_data)

            if result.get('success'):
                RouterEvent.objects.create(
                    router=router,
                    event_type='config_change',
                    message=f"Hotspot config updated: {', '.join(config_data.keys())}",
                    details={
                        'config_data': config_data,
                        'updated_by': request.user.email,
                    }
                )
                return Response({'success': True, 'message': 'Hotspot configuration updated'})
            else:
                return Response({
                    'success': False,
                    'message': 'Failed to update hotspot config',
                    'error': result.get('error'),
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        except Exception as e:
            logger.error(f"Failed to update hotspot for router {pk}: {e}")
            return Response({
                'error': 'Failed to update hotspot configuration',
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RouterBridgePortView(APIView):
    """
    POST /api/v1/network/routers/{id}/bridge/port/

    Add or remove a port from the hotspot bridge.
    Body: { "action": "add" | "remove", "interface": "ether2", "bridge": "netily-bridge" }
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]

    def post(self, request, pk):
        from django.db import connection
        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        connection.set_tenant(tenant)

        if router.status != 'online':
            return Response({
                'error': 'Router is offline',
                'message': 'Cannot modify bridge ports on an offline router'
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        action = request.data.get('action')  # 'add' or 'remove'
        interface_name = request.data.get('interface')
        bridge_name = request.data.get('bridge', 'netily-bridge')

        if action not in ('add', 'remove'):
            return Response({
                'error': 'Invalid action',
                'message': 'action must be "add" or "remove"'
            }, status=status.HTTP_400_BAD_REQUEST)

        if not interface_name:
            return Response({
                'error': 'Missing interface',
                'message': 'interface field is required'
            }, status=status.HTTP_400_BAD_REQUEST)

        try:
            mikrotik = mikrotik_api_module.MikrotikAPI(router)

            if action == 'add':
                result = mikrotik.add_port_to_bridge(interface_name, bridge_name)
            else:
                result = mikrotik.remove_port_from_bridge(interface_name)

            if result:  # Both methods return bool
                RouterEvent.objects.create(
                    router=router,
                    event_type='config_change',
                    message=f"Bridge port {'added' if action == 'add' else 'removed'}: {interface_name}",
                    details={
                        'action': action,
                        'interface': interface_name,
                        'bridge': bridge_name,
                        'modified_by': request.user.email,
                    }
                )
                return Response({
                    'success': True,
                    'message': f"Port {interface_name} {'added to' if action == 'add' else 'removed from'} bridge",
                })
            else:
                return Response({
                    'success': False,
                    'message': f"Failed to {action} port {interface_name}",
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        except Exception as e:
            logger.error(f"Failed to {action} bridge port for router {pk}: {e}")
            return Response({
                'error': f'Failed to {action} bridge port',
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


# ════════════════════════════════════════════════════════════════
# HOTSPOT IPAM CONFIGURATION (IP Address + Subnet)
# ════════════════════════════════════════════════════════════════

class RouterHotspotIPAMView(APIView):
    """
    GET  /api/v1/routers/{id}/hotspot/ipam/  → Current IPAM config + calculated network
    POST /api/v1/routers/{id}/hotspot/ipam/  → Preview only (dry run)
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]

    def get(self, request, pk):
        from django.db import connection
        from apps.network.services.ipam_calculator import (
            calculate_mikrotik_hotspot_network,
            VALID_BASE_IPS,
            VALID_CIDRS,
            CIDR_HOST_COUNTS,
        )

        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        connection.set_tenant(tenant)

        # Calculate current network boundaries
        calculated = calculate_mikrotik_hotspot_network(
            router.hotspot_base_ip,
            router.hotspot_subnet_cidr,
        )

        return Response({
            'base_ip': router.hotspot_base_ip,
            'subnet_cidr': router.hotspot_subnet_cidr,
            'calculated': calculated,
            'options': {
                'base_ips': [
                    {'value': '172.12.0.1',    'label': '172.12.0.1 (Recommended for Hotspot)'},
                    {'value': '192.168.88.1',  'label': '192.168.88.1 (MikroTik Default)'},
                    {'value': '192.168.0.1',   'label': '192.168.0.1 (Common Home Router)'},
                    {'value': '10.0.0.1',      'label': '10.0.0.1 (Enterprise Network)'},
                    {'value': '172.16.0.1',    'label': '172.16.0.1 (Private Network)'},
                    {'value': '192.168.100.1', 'label': '192.168.100.1 (Alternative)'},
                ],
                'cidrs': [
                    {'value': 8,  'label': '/8 (16,777,214 Hosts)'},
                    {'value': 12, 'label': '/12 (1,048,574 Hosts)'},
                    {'value': 16, 'label': '/16 (65,534 Hosts - Default)'},
                    {'value': 20, 'label': '/20 (4,094 Hosts)'},
                    {'value': 24, 'label': '/24 (254 Hosts)'},
                    {'value': 28, 'label': '/28 (14 Hosts)'},
                ],
            },
        })

    def post(self, request, pk):
        """Preview the calculated network for given base_ip + cidr (dry run, no apply)."""
        from django.db import connection
        from apps.network.services.ipam_calculator import (
            calculate_mikrotik_hotspot_network,
            validate_hotspot_ipam_input,
        )

        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        connection.set_tenant(tenant)

        base_ip = request.data.get('base_ip', '172.12.0.1')
        subnet_cidr = request.data.get('subnet_cidr', 16)

        try:
            subnet_cidr = int(subnet_cidr)
        except (ValueError, TypeError):
            return Response(
                {'error': 'subnet_cidr must be an integer'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate inputs
        validation_errors = validate_hotspot_ipam_input(base_ip, subnet_cidr)
        if validation_errors:
            return Response(
                {'error': 'Invalid input', 'details': validation_errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Calculate preview
        calculated = calculate_mikrotik_hotspot_network(base_ip, subnet_cidr)

        return Response({
            'preview': True,
            'base_ip': base_ip,
            'subnet_cidr': subnet_cidr,
            'calculated': calculated,
        })


class RouterHotspotIPAMApplyView(APIView):
    """
    POST /api/v1/routers/{id}/hotspot/ipam/apply/

    Saves the new IPAM config to the database AND applies it to the
    live MikroTik router in the strict 5-step sequence.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]

    def post(self, request, pk):
        from django.db import connection
        from apps.network.services.ipam_calculator import (
            calculate_mikrotik_hotspot_network,
            validate_hotspot_ipam_input,
        )
        from apps.network.services.mikrotik_ipam_sync import sync_hotspot_ipam_to_router

        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
        connection.set_tenant(tenant)

        # Parse & validate inputs
        base_ip = request.data.get('base_ip', '172.12.0.1')
        subnet_cidr = request.data.get('subnet_cidr', 16)

        try:
            subnet_cidr = int(subnet_cidr)
        except (ValueError, TypeError):
            return Response(
                {'error': 'subnet_cidr must be an integer'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        validation_errors = validate_hotspot_ipam_input(base_ip, subnet_cidr)
        if validation_errors:
            return Response(
                {'error': 'Invalid input', 'details': validation_errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Calculate network details to get gateway and pool range
        calculated = calculate_mikrotik_hotspot_network(base_ip, subnet_cidr)

        # Check router is online
        if router.status != 'online':
            # Save to DB only (will apply on next reconnect)
            old_ip = router.hotspot_base_ip
            old_cidr = router.hotspot_subnet_cidr
            
            router.hotspot_base_ip = base_ip
            router.hotspot_subnet_cidr = subnet_cidr
            router.gateway_cidr = f"{base_ip}/{subnet_cidr}"
            
            # (Removed the @property assignments that caused the crash)
            router.save(update_fields=['hotspot_base_ip', 'hotspot_subnet_cidr', 'gateway_cidr'])

            RouterEvent.objects.create(
                router=router,
                event_type='config_change',
                message=f"Hotspot IPAM saved (offline): {base_ip}/{subnet_cidr}",
                details={
                    'old_ip': old_ip, 'old_cidr': old_cidr,
                    'new_ip': base_ip, 'new_cidr': subnet_cidr,
                    'gateway': calculated['gateway'],
                    'pool_range': calculated['pool_range'],
                    'applied_to_router': False,
                    'saved_by': request.user.email if hasattr(request.user, 'email') else str(request.user),
                }
            )

            return Response({
                'success': True,
                'applied': False,
                'message': 'Configuration saved. Will be applied when router comes online.',
                'calculated': calculated,
            })

        # Router is online — apply to live MikroTik
        old_ip = router.hotspot_base_ip
        old_cidr = router.hotspot_subnet_cidr

        result = sync_hotspot_ipam_to_router(router, base_ip, subnet_cidr)

        if result['success']:
            # Save to DB after successful sync
            router.hotspot_base_ip = base_ip
            router.hotspot_subnet_cidr = subnet_cidr
            router.gateway_cidr = f"{base_ip}/{subnet_cidr}"
            
            router.save(update_fields=['hotspot_base_ip', 'hotspot_subnet_cidr', 'gateway_cidr'])

            RouterEvent.objects.create(
                router=router,
                event_type='config_change',
                message=f"Hotspot IPAM applied: {base_ip}/{subnet_cidr}",
                details={
                    'old_ip': old_ip, 'old_cidr': old_cidr,
                    'new_ip': base_ip, 'new_cidr': subnet_cidr,
                    'gateway': calculated['gateway'],
                    'pool_range': calculated['pool_range'],
                    'applied_to_router': True,
                    'sync_details': result.get('details', {}),
                    'applied_by': request.user.email if hasattr(request.user, 'email') else str(request.user),
                }
            )

            return Response({
                'success': True,
                'applied': True,
                'message': result['message'],
                'details': result.get('details', {}),
                'gateway': calculated['gateway'],
                'pool_range': calculated['pool_range'],
            })
        else:
            return Response({
                'success': False,
                'applied': False,
                'message': result['message'],
                'error': result.get('error'),
                'steps_completed': result.get('steps_completed', []),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class RouterPortManagerView(APIView):
    """
    GET: Scans the live router and returns all physical interfaces (Ethernet/WLAN)
         and marks which ones are currently assigned to the Hotspot/PPPoE Bridge.
    POST: Takes a list of selected interfaces and syncs them to the live router.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]

    def get(self, request, pk):
        from django.db import connection
        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=404)
        connection.set_tenant(tenant)

        # Try to sync status first, then check
        try:
            router.sync_status(force=True)
        except Exception:
            pass

        if router.status != 'online':
            return Response({'error': 'Router must be online to scan ports'}, status=400)

        try:
            api_wrapper = mikrotik_api_module.MikrotikAPI(router)
            if not api_wrapper.connect():
                return Response({'error': 'Failed to connect to router API'}, status=400)

            # 1. Fetch physical interfaces
            ethernets = list(api_wrapper._execute('/interface/ethernet'))
            wlans = list(api_wrapper._execute('/interface/wireless'))
            bridge_ports = list(api_wrapper._execute('/interface/bridge/port'))
            api_wrapper.disconnect()

            # Find which ports are currently on the netily-bridge
            active_bridge_ports = [
                p.get('interface') for p in bridge_ports 
                if p.get('bridge') == 'netily-bridge'
            ]

            available_ports = []
            
            # Format Ethernet ports
            for eth in ethernets:
                name = eth.get('name')
                # Never allow the WAN port to be added to the bridge!
                is_wan = (name == router.wan_interface)
                available_ports.append({
                    'name': name,
                    'type': 'ethernet',
                    'running': eth.get('running', 'false') == 'true',
                    'is_selected': name in active_bridge_ports,
                    'is_wan': is_wan,
                    'disabled': eth.get('disabled', 'false') == 'true'
                })

            # Format Wireless ports
            for wlan in wlans:
                name = wlan.get('name')
                available_ports.append({
                    'name': name,
                    'type': 'wireless',
                    'running': wlan.get('running', 'false') == 'true',
                    'is_selected': name in active_bridge_ports,
                    'is_wan': False,
                    'disabled': wlan.get('disabled', 'false') == 'true'
                })

            return Response({
                'router_id': router.id,
                'wan_interface': router.wan_interface,
                'ports': available_ports
            })

        except Exception as e:
            return Response({'error': str(e)}, status=500)

    def post(self, request, pk):
        from django.db import connection
        router, tenant = find_router_across_tenants(router_id=pk)
        if not router:
            return Response({'error': 'Router not found'}, status=404)
        connection.set_tenant(tenant)

        desired_ports = request.data.get('ports', [])
        if not isinstance(desired_ports, list):
            return Response({'error': 'Payload must contain a "ports" array'}, status=400)

        # Safety Check: Prevent admin from locking themselves out by bridging the WAN port
        if router.wan_interface in desired_ports:
            return Response({
                'error': f'Security Risk: You cannot add the WAN interface ({router.wan_interface}) to the hotspot bridge!'
            }, status=400)

        if router.status != 'online':
            # Save to DB for offline sync later
            router.hotspot_interfaces = desired_ports
            router.save(update_fields=['hotspot_interfaces'])
            return Response({'message': 'Saved to database. Will apply when router is online.', 'applied': False})

        # Apply to live router
        result = sync_bridge_ports_to_router(router, desired_ports)

        if result.get('success'):
            # Update DB to match live router
            router.hotspot_interfaces = desired_ports
            router.save(update_fields=['hotspot_interfaces'])
            
            RouterEvent.objects.create(
                router=router,
                event_type='config_change',
                message=f"Bridge ports updated: {', '.join(desired_ports) or 'None'}"
            )
            
            return Response({
                'success': True, 
                'message': 'Ports synchronized to router successfully!',
                'added': result.get('added'),
                'removed': result.get('removed')
            })
        else:
            return Response({'error': result.get('error')}, status=500)