"""
VPN Views - API endpoints for VPN management
"""
import logging
from django.db.models import Sum, Count, Q
from django.utils import timezone
from datetime import timedelta
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.http import HttpResponse

from apps.core.permissions import HasCompanyAccess
from .models import (
    CertificateAuthority,
    VPNCertificate,
    VPNConnection,
    VPNServer,
    VPNConnectionLog
)
from .serializers import (
    CertificateAuthoritySerializer,
    CertificateAuthorityDetailSerializer,
    VPNCertificateSerializer,
    VPNCertificateDetailSerializer,
    VPNCertificateCreateSerializer,
    VPNServerSerializer,
    VPNConnectionSerializer,
    VPNConnectionLogSerializer,
    VPNDashboardStatsSerializer,
    RouterVPNStatusSerializer
)
from .services import CertificateService

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────
# VPN DASHBOARD
# ────────────────────────────────────────────────────────────────

class VPNDashboardView(APIView):
    """
    GET /api/v1/vpn/dashboard/
    
    VPN Dashboard statistics and overview.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get(self, request):
        # Server stats
        total_servers = VPNServer.objects.count()
        active_servers = VPNServer.objects.filter(status='running').count()
        
        # Certificate stats
        total_certificates = VPNCertificate.objects.count()
        active_certificates = VPNCertificate.objects.filter(status='active').count()
        
        # Certificates expiring in 30 days
        expiry_threshold = timezone.now() + timedelta(days=30)
        expiring_soon = VPNCertificate.objects.filter(
            status='active',
            valid_until__lte=expiry_threshold
        ).count()
        
        revoked_certificates = VPNCertificate.objects.filter(status='revoked').count()
        
        # Connection stats
        total_connections = VPNConnection.objects.count()
        active_connections = VPNConnection.objects.filter(status='connected').count()
        
        # Traffic stats
        traffic = VPNConnection.objects.aggregate(
            total_sent=Sum('bytes_sent'),
            total_received=Sum('bytes_received')
        )
        
        stats = {
            'total_servers': total_servers,
            'active_servers': active_servers,
            'total_certificates': total_certificates,
            'active_certificates': active_certificates,
            'expiring_soon': expiring_soon,
            'revoked_certificates': revoked_certificates,
            'total_connections': total_connections,
            'active_connections': active_connections,
            'total_bytes_sent': traffic['total_sent'] or 0,
            'total_bytes_received': traffic['total_received'] or 0,
        }
        
        serializer = VPNDashboardStatsSerializer(stats)
        return Response(serializer.data)


class VPNActiveConnectionsView(APIView):
    """
    GET /api/v1/vpn/connections/active/
    
    List all active VPN connections.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get(self, request):
        connections = VPNConnection.objects.filter(
            status='connected'
        ).select_related('router', 'server').order_by('-connected_at')
        
        serializer = VPNConnectionSerializer(connections, many=True)
        return Response({
            'count': connections.count(),
            'connections': serializer.data
        })


# ────────────────────────────────────────────────────────────────
# CERTIFICATE AUTHORITY VIEWSET
# ────────────────────────────────────────────────────────────────

class CertificateAuthorityViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Certificate Authority management.
    """
    queryset = CertificateAuthority.objects.all()
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get_serializer_class(self):
        if self.action == 'retrieve':
            return CertificateAuthorityDetailSerializer
        return CertificateAuthoritySerializer
    
    def create(self, request, *args, **kwargs):
        """Create a new Certificate Authority with generated certificates"""
        name = request.data.get('name')
        common_name = request.data.get('common_name', name)
        organization = request.data.get('organization', 'Netily ISP')
        country = request.data.get('country', 'KE')
        validity_days = request.data.get('validity_days', 3650)
        
        if not name:
            return Response(
                {'error': 'Name is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if CertificateAuthority.objects.filter(name=name).exists():
            return Response(
                {'error': f'CA with name {name} already exists'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            service = CertificateService()
            ca = service.create_ca(
                name=name,
                common_name=common_name,
                organization=organization,
                country=country,
                validity_days=validity_days
            )
            
            serializer = CertificateAuthorityDetailSerializer(ca)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Failed to create CA: {e}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['get'])
    def certificates(self, request, pk=None):
        """List all certificates issued by this CA"""
        ca = self.get_object()
        certs = ca.certificates.all().order_by('-created_at')
        serializer = VPNCertificateSerializer(certs, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def crl(self, request, pk=None):
        """Download Certificate Revocation List"""
        ca = self.get_object()
        
        try:
            service = CertificateService()
            crl_pem = service.generate_crl(ca)
            
            response = HttpResponse(crl_pem, content_type='application/x-pem-file')
            response['Content-Disposition'] = f'attachment; filename="{ca.name}-crl.pem"'
            return response
            
        except Exception as e:
            logger.error(f"Failed to generate CRL: {e}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'])
    def generate_server_cert(self, request, pk=None):
        """Generate a server certificate"""
        ca = self.get_object()
        common_name = request.data.get('common_name', 'vpn-server')
        validity_days = request.data.get('validity_days', 825)
        
        try:
            service = CertificateService()
            cert = service.generate_server_certificate(
                ca=ca,
                common_name=common_name,
                validity_days=validity_days
            )
            
            serializer = VPNCertificateDetailSerializer(cert)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Failed to generate server certificate: {e}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ────────────────────────────────────────────────────────────────
# VPN CERTIFICATE VIEWSET
# ────────────────────────────────────────────────────────────────

class VPNCertificateViewSet(viewsets.ModelViewSet):
    """
    ViewSet for VPN Certificate management.
    """
    queryset = VPNCertificate.objects.all()
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return VPNCertificateCreateSerializer
        if self.action == 'retrieve':
            return VPNCertificateDetailSerializer
        return VPNCertificateSerializer
    
    def get_queryset(self):
        qs = super().get_queryset()
        
        # Filter by certificate type
        cert_type = self.request.query_params.get('type')
        if cert_type:
            qs = qs.filter(certificate_type=cert_type)
        
        # Filter by status
        cert_status = self.request.query_params.get('status')
        if cert_status:
            qs = qs.filter(status=cert_status)
        
        # Filter by router
        router_id = self.request.query_params.get('router')
        if router_id:
            qs = qs.filter(router_id=router_id)
        
        return qs.select_related('ca', 'router')
    
    def create(self, request, *args, **kwargs):
        """Generate a new certificate"""
        ca_id = request.data.get('ca')
        router_id = request.data.get('router')
        common_name = request.data.get('common_name')
        cert_type = request.data.get('certificate_type', 'client')
        validity_days = request.data.get('validity_days', 365)
        
        try:
            ca = CertificateAuthority.objects.get(id=ca_id)
        except CertificateAuthority.DoesNotExist:
            return Response(
                {'error': 'Certificate Authority not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        service = CertificateService()
        
        try:
            if cert_type == 'server':
                cert = service.generate_server_certificate(
                    ca=ca,
                    common_name=common_name or 'vpn-server',
                    validity_days=validity_days
                )
            else:
                # Client certificate
                router = None
                if router_id:
                    from apps.network.models import Router
                    try:
                        router = Router.objects.get(id=router_id)
                    except Router.DoesNotExist:
                        return Response(
                            {'error': 'Router not found'},
                            status=status.HTTP_404_NOT_FOUND
                        )
                
                cert = service.generate_client_certificate(
                    ca=ca,
                    router=router,
                    common_name=common_name,
                    validity_days=validity_days
                )
            
            serializer = VPNCertificateDetailSerializer(cert)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Failed to generate certificate: {e}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'])
    def revoke(self, request, pk=None):
        """Revoke a certificate"""
        cert = self.get_object()
        reason = request.data.get('reason', 'Revoked by administrator')
        
        if cert.status == 'revoked':
            return Response(
                {'error': 'Certificate is already revoked'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            service = CertificateService()
            service.revoke_certificate(cert, reason)
            
            serializer = VPNCertificateSerializer(cert)
            return Response(serializer.data)
            
        except Exception as e:
            logger.error(f"Failed to revoke certificate: {e}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['get'])
    def download_config(self, request, pk=None):
        """Download OpenVPN client configuration"""
        cert = self.get_object()
        
        if cert.certificate_type != 'client':
            return Response(
                {'error': 'Only client certificates can be downloaded as config'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if cert.status != 'active':
            return Response(
                {'error': 'Certificate is not active'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get server details
        server = VPNServer.objects.filter(ca=cert.ca, status='running').first()
        if not server:
            server_address = request.query_params.get('server', 'vpn.example.com')
            server_port = int(request.query_params.get('port', 1194))
            protocol = request.query_params.get('protocol', 'udp')
        else:
            server_address = server.server_address
            server_port = server.port
            protocol = server.protocol
        
        try:
            service = CertificateService()
            config = service.generate_client_config(
                certificate=cert,
                server_address=server_address,
                server_port=server_port,
                protocol=protocol
            )
            
            filename = f"netily-{cert.common_name}.ovpn"
            response = HttpResponse(config, content_type='application/x-openvpn-profile')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
            
        except Exception as e:
            logger.error(f"Failed to generate config: {e}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ────────────────────────────────────────────────────────────────
# VPN SERVER VIEWSET
# ────────────────────────────────────────────────────────────────

class VPNServerViewSet(viewsets.ModelViewSet):
    """
    ViewSet for VPN Server management.
    """
    queryset = VPNServer.objects.all()
    serializer_class = VPNServerSerializer
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    @action(detail=True, methods=['get'])
    def connections(self, request, pk=None):
        """List connections to this server"""
        server = self.get_object()
        connections = server.connections.all().order_by('-connected_at')
        
        # Filter by status
        conn_status = request.query_params.get('status')
        if conn_status:
            connections = connections.filter(status=conn_status)
        
        serializer = VPNConnectionSerializer(connections, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def status(self, request, pk=None):
        """Get server status"""
        server = self.get_object()
        
        # Update connected clients count
        active_count = server.connections.filter(status='connected').count()
        server.connected_clients = active_count
        server.last_status_check = timezone.now()
        server.save(update_fields=['connected_clients', 'last_status_check'])
        
        return Response({
            'server_id': str(server.id),
            'name': server.name,
            'status': server.status,
            'connected_clients': server.connected_clients,
            'max_clients': server.max_clients,
            'last_status_check': server.last_status_check,
        })
    
    @action(detail=True, methods=['get'])
    def config(self, request, pk=None):
        """Download server configuration"""
        server = self.get_object()
        
        if not server.certificate:
            return Response(
                {'error': 'Server has no certificate assigned'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            service = CertificateService()
            config = service.generate_server_config(
                server_cert=server.certificate,
                vpn_network=server.vpn_network,
                port=server.port,
                protocol=server.protocol
            )
            
            response = HttpResponse(config, content_type='text/plain')
            response['Content-Disposition'] = f'attachment; filename="server.conf"'
            return response
            
        except Exception as e:
            logger.error(f"Failed to generate server config: {e}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ────────────────────────────────────────────────────────────────
# VPN CONNECTION VIEWSET
# ────────────────────────────────────────────────────────────────

class VPNConnectionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for VPN Connections (read-only).
    Connections are managed by the VPN server.
    """
    queryset = VPNConnection.objects.all()
    serializer_class = VPNConnectionSerializer
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get_queryset(self):
        qs = super().get_queryset()
        
        # Filter by status
        conn_status = self.request.query_params.get('status')
        if conn_status:
            qs = qs.filter(status=conn_status)
        
        # Filter by router
        router_id = self.request.query_params.get('router')
        if router_id:
            qs = qs.filter(router_id=router_id)
        
        # Filter by server
        server_id = self.request.query_params.get('server')
        if server_id:
            qs = qs.filter(server_id=server_id)
        
        return qs.select_related('router', 'server', 'certificate')


# ────────────────────────────────────────────────────────────────
# VPN CONNECTION LOG VIEWSET
# ────────────────────────────────────────────────────────────────

class VPNConnectionLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for VPN Connection Logs (read-only).
    """
    queryset = VPNConnectionLog.objects.all()
    serializer_class = VPNConnectionLogSerializer
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get_queryset(self):
        qs = super().get_queryset()
        
        # Filter by event type
        event_type = self.request.query_params.get('event_type')
        if event_type:
            qs = qs.filter(event_type=event_type)
        
        # Filter by router
        router_id = self.request.query_params.get('router')
        if router_id:
            qs = qs.filter(router_id=router_id)
        
        # Limit results
        limit = self.request.query_params.get('limit', 100)
        try:
            limit = min(int(limit), 1000)
        except ValueError:
            limit = 100
        
        return qs.select_related('router', 'connection')[:limit]


# ────────────────────────────────────────────────────────────────
# ROUTER VPN STATUS
# ────────────────────────────────────────────────────────────────

class RouterVPNStatusView(APIView):
    """
    GET /api/v1/vpn/routers/{router_id}/status/
    
    Get VPN status for a specific router.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get(self, request, router_id):
        from apps.network.models import Router
        
        try:
            router = Router.objects.get(id=router_id)
        except Router.DoesNotExist:
            return Response(
                {'error': 'Router not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get active certificate
        cert = VPNCertificate.objects.filter(
            router=router,
            status='active'
        ).first()
        
        # Get current connection
        connection = VPNConnection.objects.filter(
            router=router,
            status='connected'
        ).first()
        
        # Get last connection if not currently connected
        last_connection = None
        if not connection:
            last_connection = VPNConnection.objects.filter(
                router=router
            ).order_by('-connected_at').first()
        
        status_data = {
            'router_id': str(router.id),
            'router_name': router.name,
            'vpn_enabled': router.enable_openvpn,
            'has_certificate': cert is not None,
            'certificate_status': cert.status if cert else None,
            'certificate_expires': cert.valid_until if cert else None,
            'connection_status': connection.status if connection else 'disconnected',
            'vpn_ip': connection.vpn_ip if connection else None,
            'last_connected': (
                connection.connected_at if connection
                else (last_connection.connected_at if last_connection else None)
            ),
        }
        
        serializer = RouterVPNStatusSerializer(status_data)
        return Response(serializer.data)


# ────────────────────────────────────────────────────────────────
# GENERATE ROUTER CERTIFICATE
# ────────────────────────────────────────────────────────────────

class GenerateRouterCertificateView(APIView):
    """
    POST /api/v1/vpn/routers/{router_id}/generate-certificate/
    
    Generate a VPN certificate for a router.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def post(self, request, router_id):
        from apps.network.models import Router
        
        try:
            router = Router.objects.get(id=router_id)
        except Router.DoesNotExist:
            return Response(
                {'error': 'Router not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get or create CA
        ca_id = request.data.get('ca')
        if ca_id:
            try:
                ca = CertificateAuthority.objects.get(id=ca_id)
            except CertificateAuthority.DoesNotExist:
                return Response(
                    {'error': 'Certificate Authority not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
        else:
            # Use first active CA
            ca = CertificateAuthority.objects.filter(is_active=True).first()
            if not ca:
                return Response(
                    {'error': 'No active Certificate Authority found. Create one first.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        # Check if router already has an active certificate
        existing_cert = VPNCertificate.objects.filter(
            router=router,
            status='active'
        ).first()
        
        if existing_cert and not request.data.get('force', False):
            return Response(
                {
                    'error': 'Router already has an active certificate',
                    'certificate_id': str(existing_cert.id),
                    'hint': 'Use force=true to revoke existing and create new'
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            service = CertificateService()
            
            # Revoke existing if forcing
            if existing_cert:
                service.revoke_certificate(existing_cert, 'Replaced by new certificate')
            
            # Generate new certificate
            cert = service.generate_client_certificate(
                ca=ca,
                router=router,
                validity_days=request.data.get('validity_days', 365)
            )
            
            # Update router VPN settings
            router.enable_openvpn = True
            router.save(update_fields=['enable_openvpn'])
            
            serializer = VPNCertificateDetailSerializer(cert)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Failed to generate router certificate: {e}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
