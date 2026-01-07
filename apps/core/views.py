"""
Core views for ISP Management System
"""
from rest_framework import viewsets, status, generics, permissions
from rest_framework.response import Response
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from django.utils import timezone
from django.conf import settings
from rest_framework_simplejwt.views import TokenRefreshView
from rest_framework.views import APIView
from rest_framework import generics
from .models import GlobalSystemSettings  # Add this
from .serializers import GlobalSystemSettingsSerializer, CustomTokenRefreshSerializer  # Add this
from rest_framework_simplejwt.exceptions import InvalidToken  # Already needed for token fix

from .models import User, Company, SystemSettings, AuditLog, Tenant
from .serializers import (
    CustomTokenRefreshSerializer, UserSerializer, LoginSerializer, UserCreateSerializer, UserUpdateSerializer,
    ProfileSerializer, PasswordChangeSerializer,
    CompanySerializer, TenantSerializer, SystemSettingsSerializer, AuditLogSerializer,
    CustomTokenObtainPairSerializer, DashboardStatsSerializer
)
from .permissions import IsAdmin, IsAdminOrStaff, IsCustomer, IsTechnician


class CustomTokenObtainPairView(TokenObtainPairView):
    """Custom JWT token view with additional user data"""
    serializer_class = CustomTokenObtainPairSerializer


class RegisterView(generics.CreateAPIView):
    """View for user registration"""
    permission_classes = [AllowAny]
    serializer_class = UserCreateSerializer
    
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            
            # Generate JWT tokens
            refresh = RefreshToken.for_user(user)
            
            # Log the action
            AuditLog.log_action(
                user=user,
                action='create',
                model_name='User',
                object_id=str(user.id),
                object_repr=str(user),
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', '')
            )
            
            return Response({
                'user': UserSerializer(user).data,
                'refresh': str(refresh),
                'access': str(refresh.access_token),
                'message': 'User registered successfully'
            }, status=status.HTTP_201_CREATED)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserViewSet(viewsets.ModelViewSet):
    """
    ViewSet for User management
    """
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def get_serializer_class(self):
        if self.action == 'create':
            return UserCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return UserUpdateSerializer
        return UserSerializer
    
    def get_permissions(self):
        if self.action in ['create', 'destroy']:
            permission_classes = [IsAuthenticated, IsAdmin]
        elif self.action in ['update', 'partial_update']:
            permission_classes = [IsAuthenticated, IsAdminOrStaff]
        else:
            permission_classes = [IsAuthenticated, IsAdminOrStaff]
        return [permission() for permission in permission_classes]
    
    @action(detail=False, methods=['get'])
    def me(self, request):
        """Get current user profile"""
        serializer = ProfileSerializer(request.user)
        return Response(serializer.data)
    
    @action(detail=False, methods=['put', 'patch'])
    def update_profile(self, request):
        """Update current user profile"""
        serializer = UserUpdateSerializer(
            request.user, 
            data=request.data, 
            partial=True,
            context={'request': request}
        )
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['post'])
    def change_password(self, request):
        """Change user password"""
        serializer = PasswordChangeSerializer(
            data=request.data,
            context={'request': request}
        )
        if serializer.is_valid():
            user = request.user
            user.set_password(serializer.validated_data['new_password'])
            user.save()
            
            # Log the action
            AuditLog.log_action(
                user=request.user,
                action='password_change',
                model_name='User',
                object_id=str(user.id),
                object_repr=str(user),
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', '')
            )
            
            return Response({'message': 'Password updated successfully'})
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CompanyViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Company management
    """
    queryset = Company.objects.all()
    serializer_class = CompanySerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def get_permissions(self):
        if self.action in ['create', 'destroy']:
            permission_classes = [IsAuthenticated, IsAdmin]
        return [permission() for permission in permission_classes]


class TenantViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Tenant management
    """
    queryset = Tenant.objects.all()
    serializer_class = TenantSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def get_permissions(self):
        if self.action in ['create', 'destroy']:
            permission_classes = [IsAuthenticated, IsAdmin]
        return [permission() for permission in permission_classes]
    
    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """Activate a tenant"""
        tenant = self.get_object()
        tenant.is_active = True
        tenant.save()
        
        AuditLog.log_action(
            user=request.user,
            action='activate',
            model_name='Tenant',
            object_id=str(tenant.id),
            object_repr=str(tenant),
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', '')
        )
        
        return Response({'message': 'Tenant activated successfully'})
    
    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        """Deactivate a tenant"""
        tenant = self.get_object()
        tenant.is_active = False
        tenant.save()
        
        AuditLog.log_action(
            user=request.user,
            action='deactivate',
            model_name='Tenant',
            object_id=str(tenant.id),
            object_repr=str(tenant),
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', '')
        )
        
        return Response({'message': 'Tenant deactivated successfully'})


class SystemSettingsViewSet(viewsets.ModelViewSet):
    """
    ViewSet for System Settings management
    """
    queryset = SystemSettings.objects.all()
    serializer_class = SystemSettingsSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            permission_classes = [IsAuthenticated, IsAdmin]
        return [permission() for permission in permission_classes]
    
    @action(detail=False, methods=['get'])
    def public(self, request):
        """Get public system settings"""
        public_settings = SystemSettings.objects.filter(is_public=True)
        serializer = self.get_serializer(public_settings, many=True)
        return Response(serializer.data)


class LoginView(generics.GenericAPIView):
    """Legacy login view using email and password"""
    serializer_class = LoginSerializer
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class LogoutView(APIView):
    """Logout view"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # In JWT, logout is handled client-side by removing tokens
        # We can blacklist the refresh token if using token blacklist app
        # For now, just return success
        return Response(
            {'message': 'Successfully logged out. Please remove tokens client-side.'},
            status=status.HTTP_200_OK
        )


class PasswordChangeView(generics.GenericAPIView):
    """Change password - matches frontend /auth/change-password/"""
    serializer_class = PasswordChangeSerializer
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            
            # Log the action
            AuditLog.log_action(
                user=request.user,
                action='password_change',
                model_name='User',
                object_id=str(request.user.id),
                object_repr=str(request.user),
                ip_address=request.META.get('REMOTE_ADDR'),
                user_agent=request.META.get('HTTP_USER_AGENT', '')
            )
            
            return Response({'message': 'Password changed successfully'})
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class VerifyEmailView(APIView):
    """Verify email view"""
    permission_classes = [AllowAny]

    def get(self, request, token):
        # Placeholder implementation - you need to implement email verification logic
        return Response(
            {'message': 'Email verification endpoint. Implement verification logic.'},
            status=status.HTTP_200_OK
        )


class ResendVerificationView(APIView):
    """Resend verification email view"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Placeholder implementation
        return Response(
            {'message': 'Resend verification endpoint. Implement resend logic.'},
            status=status.HTTP_200_OK
        )


class ProfileView(generics.RetrieveUpdateAPIView):
    """User profile view"""
    serializer_class = ProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class DashboardView(APIView):
    """Dashboard view (class-based version)"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Reuse the logic from function-based dashboard
        user = request.user
        
        if user.role == 'admin' or user.is_superuser:
            stats = {
                'total_users': User.objects.count(),
                'total_companies': Company.objects.count(),
                'total_customers': User.objects.filter(role='customer').count(),
                'total_staff': User.objects.filter(
                    role__in=['admin', 'staff', 'technician', 'accountant', 'support']
                ).count(),
                'recent_activity': list(AuditLog.objects.all().order_by('-timestamp')[:10].values(
                    'id', 'user__email', 'action', 'model_name', 'object_repr', 'timestamp'
                )),
            }
        elif user.role == 'staff':
            stats = {
                'total_customers': User.objects.filter(role='customer').count(),
                'recent_activity': list(AuditLog.objects.all().order_by('-timestamp')[:10].values(
                    'id', 'user__email', 'action', 'model_name', 'object_repr', 'timestamp'
                )),
            }
        else:
            stats = {
                'user_info': ProfileSerializer(user).data,
            }
        
        serializer = DashboardStatsSerializer(stats)
        return Response(serializer.data)

# Keep the function-based views as well for compatibility
@api_view(['POST'])
@permission_classes([AllowAny])
def register_user(request):
    """
    Register a new user (function-based view for compatibility)
    """
    serializer = UserCreateSerializer(data=request.data)
    if serializer.is_valid():
        user = serializer.save()
        
        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)
        
        # Log the action
        AuditLog.log_action(
            user=user,
            action='create',
            model_name='User',
            object_id=str(user.id),
            object_repr=str(user),
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', '')
        )
        
        return Response({
            'user': UserSerializer(user).data,
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'message': 'User registered successfully'
        }, status=status.HTTP_201_CREATED)
    
    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard(request):
    """
    Get dashboard statistics
    """
    user = request.user
    
    if user.role == 'admin' or user.is_superuser:
        # Admin dashboard
        stats = {
            'total_users': User.objects.count(),
            'total_companies': Company.objects.count(),
            'total_customers': User.objects.filter(role='customer').count(),
            'total_staff': User.objects.filter(role__in=['admin', 'staff', 'technician', 'accountant', 'support']).count(),
            'recent_activity': AuditLog.objects.all().order_by('-timestamp')[:10].values(
                'id', 'user__email', 'action', 'model_name', 'object_repr', 'timestamp'
            ),
        }
    elif user.role == 'staff':
        # Staff dashboard
        stats = {
            'total_customers': User.objects.filter(role='customer').count(),
            'recent_activity': AuditLog.objects.all().order_by('-timestamp')[:10].values(
                'id', 'user__email', 'action', 'model_name', 'object_repr', 'timestamp'
            ),
        }
    else:
        # Customer dashboard
        stats = {
            'user_info': ProfileSerializer(user).data,
        }
    
    serializer = DashboardStatsSerializer(stats)
    return Response(serializer.data)


@api_view(['GET'])
@permission_classes([AllowAny])
def health_check(request):
    """
    Health check endpoint
    """
    return Response({
        'status': 'healthy',
        'timestamp': timezone.now(),
        'version': '1.0.0'
    })


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for Audit Log (read-only)
    """
    queryset = AuditLog.objects.all().order_by('-timestamp')
    serializer_class = AuditLogSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # Apply filters from query parameters
        user_id = self.request.query_params.get('user_id')
        if user_id:
            queryset = queryset.filter(user_id=user_id)
        
        action = self.request.query_params.get('action')
        if action:
            queryset = queryset.filter(action=action)
        
        model_name = self.request.query_params.get('model_name')
        if model_name:
            queryset = queryset.filter(model_name=model_name)
        
        date_from = self.request.query_params.get('date_from')
        if date_from:
            queryset = queryset.filter(timestamp__date__gte=date_from)
        
        date_to = self.request.query_params.get('date_to')
        if date_to:
            queryset = queryset.filter(timestamp__date__lte=date_to)
        
        return queryset
    

class GlobalSystemSettingsView(APIView):
    """Singleton System Settings - GET and PATCH /api/v1/core/settings/"""
    permission_classes = [IsAdmin]

    def get_object(self):
        return GlobalSystemSettings.get_solo()

    def get(self, request):
        settings = self.get_object()
        serializer = GlobalSystemSettingsSerializer(settings)
        return Response(serializer.data)

    def patch(self, request):
        settings = self.get_object()
        serializer = GlobalSystemSettingsSerializer(settings, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CustomTokenRefreshView(TokenRefreshView):
    """Fix: Return 401 instead of 500 when user is deleted"""
    serializer_class = CustomTokenRefreshSerializer