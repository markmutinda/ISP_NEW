"""
RADIUS Views - API endpoints for RADIUS management
"""
import logging
from datetime import timedelta
from django.db.models import Sum, Count, Avg, Q
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.permissions import HasCompanyAccess
from .models import (
    RadCheck,
    RadReply,
    RadUserGroup,
    RadGroupCheck,
    RadGroupReply,
    RadAcct,
    Nas,
    RadPostAuth,
    RadiusBandwidthProfile,
    RadiusTenantConfig,
    CustomerRadiusCredentials,
)
from .serializers import (
    RadCheckSerializer,
    RadReplySerializer,
    RadUserGroupSerializer,
    RadGroupCheckSerializer,
    RadGroupReplySerializer,
    RadAcctSerializer,
    RadAcctSummarySerializer,
    NasSerializer,
    NasDetailSerializer,
    RadPostAuthSerializer,
    RadiusBandwidthProfileSerializer,
    RadiusUserCreateSerializer,
    RadiusDashboardSerializer,
    RadiusTenantConfigSerializer,
    RadiusTenantConfigDetailSerializer,
    CustomerRadiusCredentialsSerializer,
    CustomerRadiusCredentialsDetailSerializer,
)
from .services import RadiusSyncService

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────
# RADIUS DASHBOARD
# ────────────────────────────────────────────────────────────────

class RadiusDashboardView(APIView):
    """
    GET /api/v1/radius/dashboard/
    
    RADIUS Dashboard statistics and overview.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get(self, request):
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday = now - timedelta(days=1)
        
        # User stats
        total_users = RadCheck.objects.filter(
            attribute='Cleartext-Password'
        ).values('username').distinct().count()
        
        # Active sessions
        active_sessions = RadAcct.objects.filter(acctstoptime__isnull=True).count()
        
        # NAS stats
        total_nas = Nas.objects.count()
        
        # Profile stats
        total_profiles = RadiusBandwidthProfile.objects.filter(is_active=True).count()
        
        # Auth stats (last 24h)
        auth_success_24h = RadPostAuth.objects.filter(
            authdate__gte=yesterday,
            reply='Access-Accept'
        ).count()
        
        auth_failure_24h = RadPostAuth.objects.filter(
            authdate__gte=yesterday,
            reply='Access-Reject'
        ).count()
        
        # Traffic stats (today)
        traffic_today = RadAcct.objects.filter(
            acctstarttime__gte=today_start
        ).aggregate(
            bytes_in=Sum('acctinputoctets'),
            bytes_out=Sum('acctoutputoctets')
        )
        
        # Top users by traffic (last 24h)
        top_users = RadAcct.objects.filter(
            acctstarttime__gte=yesterday
        ).values('username').annotate(
            total_bytes=Sum('acctinputoctets') + Sum('acctoutputoctets'),
            sessions=Count('radacctid')
        ).order_by('-total_bytes')[:10]
        
        data = {
            'total_users': total_users,
            'active_sessions': active_sessions,
            'total_nas': total_nas,
            'total_profiles': total_profiles,
            'auth_success_24h': auth_success_24h,
            'auth_failure_24h': auth_failure_24h,
            'bytes_in_today': traffic_today['bytes_in'] or 0,
            'bytes_out_today': traffic_today['bytes_out'] or 0,
            'top_users': list(top_users)
        }
        
        serializer = RadiusDashboardSerializer(data)
        return Response(serializer.data)


class RadiusActiveSessionsView(APIView):
    """
    GET /api/v1/radius/sessions/active/
    
    List all active RADIUS sessions.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get(self, request):
        sessions = RadAcct.objects.filter(
            acctstoptime__isnull=True
        ).select_related('customer', 'router').order_by('-acctstarttime')
        
        # Filter by NAS
        nas_ip = request.query_params.get('nas')
        if nas_ip:
            sessions = sessions.filter(nasipaddress=nas_ip)
        
        # Filter by username
        username = request.query_params.get('username')
        if username:
            sessions = sessions.filter(username__icontains=username)
        
        serializer = RadAcctSerializer(sessions[:100], many=True)
        return Response({
            'count': sessions.count(),
            'sessions': serializer.data
        })


# ────────────────────────────────────────────────────────────────
# RADIUS USER MANAGEMENT
# ────────────────────────────────────────────────────────────────

class RadiusUserView(APIView):
    """
    RADIUS User management endpoints.
    
    POST /api/v1/radius/users/ - Create user
    GET /api/v1/radius/users/{username}/ - Get user details
    PUT /api/v1/radius/users/{username}/ - Update user
    DELETE /api/v1/radius/users/{username}/ - Delete user
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def post(self, request):
        """Create a new RADIUS user"""
        serializer = RadiusUserCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        
        # Handle password: use provided or auto-generate
        from utils.helpers import generate_random_password
        
        password = data.get('password')
        auto_generate = data.get('auto_generate_password', False)
        
        if auto_generate or not password:
            password = generate_random_password(length=12)
        
        # Get customer if specified
        customer = None
        if data.get('customer_id'):
            from apps.customers.models import Customer
            try:
                customer = Customer.objects.get(id=data['customer_id'])
            except Customer.DoesNotExist:
                return Response(
                    {'error': 'Customer not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
        
        # Get profile if specified
        profile = None
        if data.get('profile_id'):
            try:
                profile = RadiusBandwidthProfile.objects.get(id=data['profile_id'])
            except RadiusBandwidthProfile.DoesNotExist:
                return Response(
                    {'error': 'Bandwidth profile not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
        
        # Build attributes
        check_attrs = {}
        reply_attrs = {}
        
        if data.get('expiration'):
            check_attrs['Expiration'] = data['expiration'].strftime("%b %d %Y %H:%M:%S")
        
        if data.get('simultaneous_use'):
            check_attrs['Simultaneous-Use'] = str(data['simultaneous_use'])
        
        if data.get('download_speed') and data.get('upload_speed'):
            reply_attrs['Mikrotik-Rate-Limit'] = f"{data['upload_speed']}k/{data['download_speed']}k"
        
        if data.get('static_ip'):
            reply_attrs['Framed-IP-Address'] = str(data['static_ip'])
        
        if data.get('session_timeout'):
            reply_attrs['Session-Timeout'] = str(data['session_timeout'])
        
        try:
            service = RadiusSyncService()
            result = service.create_radius_user(
                username=data['username'],
                password=password,
                customer=customer,
                profile=profile,
                attributes=check_attrs,
                reply_attributes=reply_attrs,
                groupname=data.get('groupname')
            )
            
            # Include password in response so admin can see it
            result['password'] = password
            
            return Response(result, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"Failed to create RADIUS user: {e}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def get(self, request, username=None):
        """Get RADIUS user details"""
        if not username:
            # List all users with proper paginated response format
            users = RadCheck.objects.filter(
                attribute='Cleartext-Password'
            ).values('username').distinct()
            
            # Apply search filter if provided
            search = request.query_params.get('search')
            if search:
                users = users.filter(username__icontains=search)
            
            # Get pagination params
            page = int(request.query_params.get('page', 1))
            page_size = int(request.query_params.get('page_size', 50))
            
            total_count = users.count()
            start = (page - 1) * page_size
            end = start + page_size
            
            user_list = []
            for u in users[start:end]:
                uname = u['username']
                checks = RadCheck.objects.filter(username=uname)
                replies = RadReply.objects.filter(username=uname)
                groups = RadUserGroup.objects.filter(username=uname)
                
                # Check if user is disabled
                is_disabled = checks.filter(
                    attribute='Auth-Type',
                    value='Reject'
                ).exists()
                
                # Get password from Cleartext-Password attribute
                password = None
                password_entry = checks.filter(attribute='Cleartext-Password').first()
                if password_entry:
                    password = password_entry.value
                
                # Get speed settings from replies
                download_speed = 0
                upload_speed = 0
                rate_limit = replies.filter(attribute='Mikrotik-Rate-Limit').first()
                if rate_limit:
                    try:
                        parts = rate_limit.value.replace('k', '').replace('M', '000').split('/')
                        if len(parts) == 2:
                            upload_speed = int(parts[0])
                            download_speed = int(parts[1])
                    except:
                        pass
                
                # Try to get linked customer info
                customer_name = None
                customer_id = None
                try:
                    creds = CustomerRadiusCredentials.objects.filter(
                        radius_username=uname
                    ).select_related('customer').first()
                    if creds and creds.customer:
                        customer_name = creds.customer.full_name
                        customer_id = creds.customer.id
                except:
                    pass
                
                user_list.append({
                    'id': checks.first().id if checks.exists() else 0,
                    'username': uname,
                    'password': password,
                    'customer': customer_id,
                    'customer_name': customer_name,
                    'status': 'disabled' if is_disabled else 'enabled',
                    'download_speed': download_speed,
                    'upload_speed': upload_speed,
                    'check_count': checks.count(),
                    'reply_count': replies.count(),
                    'groups': list(groups.values_list('groupname', flat=True)),
                    'created_at': checks.first().created_at.isoformat() if checks.exists() and hasattr(checks.first(), 'created_at') else None,
                    'updated_at': checks.first().updated_at.isoformat() if checks.exists() and hasattr(checks.first(), 'updated_at') else None,
                })
            
            # Return DRF-style paginated response
            return Response({
                'count': total_count,
                'next': None if end >= total_count else f"?page={page + 1}&page_size={page_size}",
                'previous': None if page <= 1 else f"?page={page - 1}&page_size={page_size}",
                'results': user_list
            })
        
        # Get specific user
        checks = RadCheck.objects.filter(username=username)
        if not checks.exists():
            return Response(
                {'error': 'User not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        replies = RadReply.objects.filter(username=username)
        groups = RadUserGroup.objects.filter(username=username)
        
        # Get password
        password = None
        password_entry = checks.filter(attribute='Cleartext-Password').first()
        if password_entry:
            password = password_entry.value
        
        # Check if disabled
        is_disabled = checks.filter(
            attribute='Auth-Type',
            value='Reject'
        ).exists()
        
        return Response({
            'username': username,
            'password': password,
            'is_disabled': is_disabled,
            'check_attributes': RadCheckSerializer(checks, many=True).data,
            'reply_attributes': RadReplySerializer(replies, many=True).data,
            'groups': RadUserGroupSerializer(groups, many=True).data
        })
    
    def put(self, request, username):
        """Update RADIUS user"""
        if not RadCheck.objects.filter(username=username).exists():
            return Response(
                {'error': 'User not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        data = request.data
        service = RadiusSyncService()
        
        check_attrs = {}
        reply_attrs = {}
        
        # Handle password update
        password = data.get('password')
        
        # Handle other attributes
        if data.get('expiration'):
            check_attrs['Expiration'] = data['expiration']
        
        if data.get('simultaneous_use'):
            check_attrs['Simultaneous-Use'] = str(data['simultaneous_use'])
        
        if data.get('download_speed') and data.get('upload_speed'):
            reply_attrs['Mikrotik-Rate-Limit'] = f"{data['upload_speed']}k/{data['download_speed']}k"
        
        if data.get('static_ip'):
            reply_attrs['Framed-IP-Address'] = str(data['static_ip'])
        
        try:
            service.update_radius_user(
                username=username,
                password=password,
                attributes=check_attrs if check_attrs else None,
                reply_attributes=reply_attrs if reply_attrs else None
            )
            
            return Response({'status': 'updated', 'username': username})
            
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def delete(self, request, username):
        """Delete RADIUS user"""
        service = RadiusSyncService()
        service.delete_radius_user(username)
        return Response({'status': 'deleted', 'username': username})


class RadiusUserActionView(APIView):
    """
    RADIUS User actions (enable/disable).
    
    POST /api/v1/radius/users/{username}/enable/
    POST /api/v1/radius/users/{username}/disable/
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def post(self, request, username, action):
        service = RadiusSyncService()
        
        if action == 'enable':
            result = service.enable_radius_user(username)
            return Response({'status': 'enabled' if result else 'not_found', 'username': username})
        
        elif action == 'disable':
            reason = request.data.get('reason', 'Disabled by admin')
            result = service.disable_radius_user(username, reason)
            return Response({'status': 'disabled' if result else 'not_found', 'username': username})
        
        elif action == 'disconnect':
            # TODO: Implement CoA disconnect
            return Response({'status': 'not_implemented'}, status=status.HTTP_501_NOT_IMPLEMENTED)
        
        return Response({'error': 'Invalid action'}, status=status.HTTP_400_BAD_REQUEST)


# ────────────────────────────────────────────────────────────────
# VIEWSETS
# ────────────────────────────────────────────────────────────────

class RadAcctViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for RADIUS Accounting records (read-only)"""
    queryset = RadAcct.objects.all()
    serializer_class = RadAcctSerializer
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get_queryset(self):
        qs = super().get_queryset()
        
        # Filter by username
        username = self.request.query_params.get('username')
        if username:
            qs = qs.filter(username__icontains=username)
        
        # Filter by NAS
        nas_ip = self.request.query_params.get('nas')
        if nas_ip:
            qs = qs.filter(nasipaddress=nas_ip)
        
        # Filter by active only
        active_only = self.request.query_params.get('active')
        if active_only == 'true':
            qs = qs.filter(acctstoptime__isnull=True)
        
        # Date range filter
        start_date = self.request.query_params.get('start_date')
        if start_date:
            qs = qs.filter(acctstarttime__gte=start_date)
        
        end_date = self.request.query_params.get('end_date')
        if end_date:
            qs = qs.filter(acctstarttime__lte=end_date)
        
        return qs.select_related('customer', 'router').order_by('-acctstarttime')
    
    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Get accounting summary statistics"""
        qs = self.get_queryset()
        
        summary = qs.aggregate(
            total_sessions=Count('radacctid'),
            active_sessions=Count('radacctid', filter=Q(acctstoptime__isnull=True)),
            total_bytes_in=Sum('acctinputoctets'),
            total_bytes_out=Sum('acctoutputoctets'),
            avg_session_time=Avg('acctsessiontime'),
            unique_users=Count('username', distinct=True)
        )
        
        serializer = RadAcctSummarySerializer(summary)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def by_user(self, request):
        """Get accounting grouped by user"""
        username = request.query_params.get('username')
        if not username:
            return Response(
                {'error': 'username parameter required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        sessions = self.get_queryset().filter(username=username)
        
        summary = sessions.aggregate(
            total_sessions=Count('radacctid'),
            total_time=Sum('acctsessiontime'),
            total_bytes_in=Sum('acctinputoctets'),
            total_bytes_out=Sum('acctoutputoctets')
        )
        
        return Response({
            'username': username,
            'summary': summary,
            'recent_sessions': RadAcctSerializer(sessions[:20], many=True).data
        })


class NasViewSet(viewsets.ModelViewSet):
    """ViewSet for NAS management"""
    queryset = Nas.objects.all()
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get_serializer_class(self):
        if self.action in ['retrieve', 'create', 'update']:
            return NasDetailSerializer
        return NasSerializer
    
    def get_queryset(self):
        return Nas.objects.select_related('router').order_by('-created_at')
    
    @action(detail=False, methods=['post'])
    def sync_routers(self, request):
        """Sync all routers to NAS table"""
        service = RadiusSyncService()
        count = service.sync_all_routers()
        return Response({'status': 'success', 'synced': count})
    
    @action(detail=True, methods=['get'])
    def sessions(self, request, pk=None):
        """Get active sessions for this NAS"""
        nas = self.get_object()
        sessions = RadAcct.objects.filter(
            nasipaddress=nas.nasname,
            acctstoptime__isnull=True
        ).order_by('-acctstarttime')
        
        serializer = RadAcctSerializer(sessions, many=True)
        return Response(serializer.data)


class RadiusBandwidthProfileViewSet(viewsets.ModelViewSet):
    """ViewSet for Bandwidth Profiles"""
    queryset = RadiusBandwidthProfile.objects.all()
    serializer_class = RadiusBandwidthProfileSerializer
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get_queryset(self):
        qs = super().get_queryset()
        
        # Filter by active only
        active_only = self.request.query_params.get('active')
        if active_only == 'true':
            qs = qs.filter(is_active=True)
        
        return qs.order_by('name')
    
    @action(detail=False, methods=['post'])
    def sync_to_radius(self, request):
        """Sync all profiles to RADIUS groups"""
        service = RadiusSyncService()
        count = service.sync_all_bandwidth_profiles()
        return Response({'status': 'success', 'synced': count})
    
    @action(detail=True, methods=['get'])
    def preview(self, request, pk=None):
        """Preview RADIUS attributes for this profile"""
        profile = self.get_object()
        return Response({
            'profile': profile.name,
            'mikrotik_rate_limit': profile.mikrotik_rate_limit,
            'radius_attributes': profile.get_radius_attributes()
        })


class RadPostAuthViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for Post-Auth logs (read-only)"""
    queryset = RadPostAuth.objects.all()
    serializer_class = RadPostAuthSerializer
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get_queryset(self):
        qs = super().get_queryset()
        
        # Filter by reply type
        reply = self.request.query_params.get('reply')
        if reply:
            qs = qs.filter(reply=reply)
        
        # Filter by username
        username = self.request.query_params.get('username')
        if username:
            qs = qs.filter(username__icontains=username)
        
        # Limit
        limit = self.request.query_params.get('limit', 100)
        try:
            limit = min(int(limit), 1000)
        except ValueError:
            limit = 100
        
        return qs.order_by('-authdate')[:limit]
    
    @action(detail=False, methods=['get'])
    def failures(self, request):
        """Get recent authentication failures"""
        failures = RadPostAuth.objects.filter(
            reply='Access-Reject'
        ).order_by('-authdate')[:50]
        
        serializer = RadPostAuthSerializer(failures, many=True)
        return Response(serializer.data)


# ────────────────────────────────────────────────────────────────
# SYNC ENDPOINTS
# ────────────────────────────────────────────────────────────────

class RadiusSyncView(APIView):
    """
    Manual sync endpoints for RADIUS.
    """
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def post(self, request, sync_type):
        service = RadiusSyncService()
        
        if sync_type == 'customers':
            result = service.sync_all_customers()
            return Response({'status': 'success', 'result': result})
        
        elif sync_type == 'routers':
            count = service.sync_all_routers()
            return Response({'status': 'success', 'synced': count})
        
        elif sync_type == 'profiles':
            count = service.sync_all_bandwidth_profiles()
            return Response({'status': 'success', 'synced': count})
        
        elif sync_type == 'all':
            results = {
                'customers': service.sync_all_customers(),
                'routers': service.sync_all_routers(),
                'profiles': service.sync_all_bandwidth_profiles()
            }
            return Response({'status': 'success', 'results': results})
        
        return Response(
            {'error': f'Unknown sync type: {sync_type}'},
            status=status.HTTP_400_BAD_REQUEST
        )


# ────────────────────────────────────────────────────────────────
# TENANT RADIUS CONFIGURATION (Multi-Tenant Support)
# ────────────────────────────────────────────────────────────────

class RadiusTenantConfigViewSet(viewsets.ModelViewSet):
    """
    API ViewSet for managing RADIUS tenant configurations.
    
    Endpoints:
        GET    /api/v1/radius/tenant-config/          - List all configs
        POST   /api/v1/radius/tenant-config/          - Create new config
        GET    /api/v1/radius/tenant-config/{id}/     - Get config details
        PUT    /api/v1/radius/tenant-config/{id}/     - Update config
        DELETE /api/v1/radius/tenant-config/{id}/     - Delete config
        POST   /api/v1/radius/tenant-config/{id}/configure/  - Generate RADIUS config
        POST   /api/v1/radius/tenant-config/{id}/regenerate/ - Regenerate config files
    """
    
    queryset = RadiusTenantConfig.objects.all()
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get_serializer_class(self):
        if self.action == 'retrieve':
            return RadiusTenantConfigDetailSerializer
        return RadiusTenantConfigSerializer
    
    @action(detail=True, methods=['post'])
    def configure(self, request, pk=None):
        """
        Generate RADIUS configuration files for this tenant.
        
        POST /api/v1/radius/tenant-config/{id}/configure/
        """
        from .services.tenant_radius_service import tenant_radius_service
        
        config = self.get_object()
        
        try:
            result = tenant_radius_service.configure_tenant_radius(
                schema_name=config.schema_name,
                tenant_name=config.tenant_name
            )
            
            config.config_generated = True
            config.last_config_update = timezone.now()
            config.save(update_fields=['config_generated', 'last_config_update'])
            
            return Response({
                'status': 'success',
                'message': f'RADIUS configuration generated for {config.tenant_name}',
                'result': result
            })
            
        except Exception as e:
            logger.error(f"Failed to configure RADIUS for tenant {config.schema_name}: {e}")
            return Response(
                {'status': 'error', 'message': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'])
    def regenerate(self, request, pk=None):
        """
        Regenerate RADIUS configuration files.
        
        POST /api/v1/radius/tenant-config/{id}/regenerate/
        """
        from .services.tenant_radius_service import tenant_radius_service
        
        config = self.get_object()
        
        try:
            # Get custom settings from request
            radius_secret = request.data.get('radius_secret') or config.radius_secret
            
            result = tenant_radius_service.configure_tenant_radius(
                schema_name=config.schema_name,
                tenant_name=config.tenant_name,
                radius_secret=radius_secret
            )
            
            config.last_config_update = timezone.now()
            config.save(update_fields=['last_config_update'])
            
            return Response({
                'status': 'success',
                'message': f'RADIUS configuration regenerated for {config.tenant_name}',
                'result': result
            })
            
        except Exception as e:
            logger.error(f"Failed to regenerate RADIUS config for {config.schema_name}: {e}")
            return Response(
                {'status': 'error', 'message': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class CustomerRadiusCredentialsViewSet(viewsets.ModelViewSet):
    """
    API ViewSet for managing customer RADIUS credentials.
    
    Endpoints:
        GET    /api/v1/radius/credentials/          - List all credentials
        POST   /api/v1/radius/credentials/          - Create new credentials
        GET    /api/v1/radius/credentials/{id}/     - Get credential details
        PUT    /api/v1/radius/credentials/{id}/     - Update credentials
        DELETE /api/v1/radius/credentials/{id}/     - Delete credentials
        POST   /api/v1/radius/credentials/{id}/sync/    - Force sync to RADIUS
        POST   /api/v1/radius/credentials/{id}/enable/  - Enable account
        POST   /api/v1/radius/credentials/{id}/disable/ - Disable account
    """
    
    permission_classes = [IsAuthenticated, HasCompanyAccess]
    
    def get_queryset(self):
        return CustomerRadiusCredentials.objects.select_related(
            'customer', 'bandwidth_profile'
        ).all()
    
    def get_serializer_class(self):
        if self.action == 'retrieve':
            return CustomerRadiusCredentialsDetailSerializer
        return CustomerRadiusCredentialsSerializer
    
    @action(detail=True, methods=['post'])
    def sync(self, request, pk=None):
        """
        Force sync credentials to RADIUS tables.
        
        POST /api/v1/radius/credentials/{id}/sync/
        """
        credentials = self.get_object()
        
        try:
            credentials.sync_to_radius()
            return Response({
                'status': 'success',
                'message': f'Synced {credentials.username} to RADIUS',
                'synced_at': credentials.last_sync
            })
        except Exception as e:
            logger.error(f"Failed to sync credentials {credentials.username}: {e}")
            return Response(
                {'status': 'error', 'message': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'])
    def enable(self, request, pk=None):
        """
        Enable RADIUS account.
        
        POST /api/v1/radius/credentials/{id}/enable/
        """
        credentials = self.get_object()
        
        credentials.is_enabled = True
        credentials.disabled_reason = ''
        credentials.save()
        
        return Response({
            'status': 'success',
            'message': f'Enabled RADIUS account: {credentials.username}'
        })
    
    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        """
        Disable RADIUS account.
        
        POST /api/v1/radius/credentials/{id}/disable/
        """
        credentials = self.get_object()
        
        reason = request.data.get('reason', 'Manually disabled')
        
        credentials.is_enabled = False
        credentials.disabled_reason = reason
        credentials.save()
        
        return Response({
            'status': 'success',
            'message': f'Disabled RADIUS account: {credentials.username}'
        })
    
    @action(detail=True, methods=['post'])
    def regenerate_username(self, request, pk=None):
        """
        Regenerate username based on phone number (simplified format).
        
        POST /api/v1/radius/credentials/{id}/regenerate_username/
        """
        credentials = self.get_object()
        customer = credentials.customer
        
        # Generate new username from phone number
        phone = customer.user.phone_number or ''
        digits = ''.join(c for c in phone if c.isdigit())
        
        if len(digits) >= 9:
            new_username = digits[-9:]  # Last 9 digits (Kenya phone without +254)
        else:
            # Fallback to customer code
            new_username = customer.customer_code.lower().replace(' ', '_')[:20]
        
        old_username = credentials.username
        credentials.username = new_username
        credentials.save()
        
        # Force sync to update RADIUS tables
        try:
            credentials.sync_to_radius()
        except Exception as e:
            logger.warning(f"Failed to sync after username regeneration: {e}")
        
        return Response({
            'status': 'success',
            'old_username': old_username,
            'new_username': new_username,
            'message': f'Username regenerated from {old_username} to {new_username}'
        })

    @action(detail=True, methods=['post'])
    def renew(self, request, pk=None):
        """
        Renew subscription - extends expiration date based on current plan.
        
        POST /api/v1/radius/credentials/{id}/renew/
        
        This endpoint:
        1. Gets the customer's active service connection with a plan
        2. Calculates new expiration based on plan validity settings
        3. Updates the credentials expiration_date
        4. Re-enables the account if disabled
        5. Syncs to RADIUS tables
        
        Returns:
            {
                "status": "success",
                "message": "Subscription renewed",
                "username": "712345678",
                "new_expiration": "2026-02-06T14:30:00Z"
            }
        """
        from django.utils import timezone
        from apps.radius.signals_auto_sync import calculate_expiration_from_plan
        
        credentials = self.get_object()
        customer = credentials.customer
        
        # Find active service with a plan
        service = customer.services.filter(
            status='ACTIVE',
            auth_connection_type__in=['PPPOE', 'HOTSPOT'],
            plan__isnull=False
        ).first()
        
        if not service or not service.plan:
            # Try to find any service with a plan
            service = customer.services.filter(
                plan__isnull=False
            ).first()
        
        if not service or not service.plan:
            return Response({
                'status': 'error',
                'message': 'No plan found for this customer. Please assign a plan first.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Calculate new expiration
        new_expiration = calculate_expiration_from_plan(service.plan)
        
        # Update credentials
        credentials.expiration_date = new_expiration
        credentials.is_enabled = True
        credentials.disabled_reason = ''
        credentials.save()
        
        # Force sync to RADIUS
        try:
            credentials.sync_to_radius()
        except Exception as e:
            logger.warning(f"Failed to sync after renewal: {e}")
        
        # Format expiration for response
        expiration_str = None
        if new_expiration:
            expiration_str = new_expiration.isoformat()
        
        logger.info(
            f"Renewed subscription for {credentials.username}: "
            f"Plan={service.plan.name}, "
            f"ValidityType={service.plan.validity_type}, "
            f"NewExpiration={expiration_str or 'Unlimited'}"
        )
        
        return Response({
            'status': 'success',
            'message': f'Subscription renewed based on plan: {service.plan.name}',
            'username': credentials.username,
            'new_expiration': expiration_str,
            'plan_name': service.plan.name,
            'validity_type': service.plan.validity_type
        })
