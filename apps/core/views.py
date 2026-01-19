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
from datetime import timedelta
from .models import GlobalSystemSettings  # Add this
from .serializers import GlobalSystemSettingsSerializer, CustomTokenRefreshSerializer  # Add this
from rest_framework_simplejwt.exceptions import InvalidToken  # Already needed for token fix
from .serializers import CompanyRegisterSerializer
from django.core.mail import send_mail  # Add this for email
from django.template.loader import render_to_string  # For email template
from django.utils.html import strip_tags  # For plain text email
from .models import Domain   # ← This is your custom Domain in core/models.py
from django_tenants.utils import tenant_context, schema_context
from django.core.management import call_command

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


# In RegisterView class, update the create method:

class RegisterView(generics.CreateAPIView):
    """View for user registration"""
    permission_classes = [AllowAny]
    serializer_class = UserCreateSerializer
    
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            # Check if company/tenant should be assigned automatically
            # For now, we'll allow it to be set via request data
            # Later, we can add logic to auto-assign based on domain or other criteria
            
            user = serializer.save()
            
            # If no company was set, try to assign based on registration context
            if not user.company and not user.tenant:
                # Placeholder for auto-assignment logic
                # Example: Get company from subdomain, invite code, etc.
                pass
            
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
    ViewSet for User management - filtered by company
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
            return [IsAuthenticated(), IsAdmin()]
        elif self.action in ['update', 'partial_update', 'me', 'update_profile', 'change_password']:
            return [IsAuthenticated()]
        return [IsAuthenticated(), IsAdminOrStaff()]
    
    def get_queryset(self):
        """
        Superuser sees all users.
        Company admins/staff see only users in their company.
        """
        qs = super().get_queryset().select_related('company')
        
        if self.request.user.is_superuser:
            # Optional: allow filtering by company via query param
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return qs.filter(company_id=company_id)
            return qs
        
        # Company users only see their own company
        if hasattr(self.request.user, 'company') and self.request.user.company:
            return qs.filter(company=self.request.user.company)
        
        # Fallback: nothing
        return qs.none()
    
    def perform_create(self, serializer):
        """
        When creating a user, auto-set company to current user's company
        (unless superuser explicitly sets another)
        """
        if self.request.user.is_superuser:
            # Superuser can set any company
            serializer.save()
        else:
            # Normal company admin/staff → force their company
            if hasattr(self.request.user, 'company') and self.request.user.company:
                serializer.save(company=self.request.user.company)
            else:
                serializer.save()  # fallback
    
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


# In DashboardView class, update the get method:

class DashboardView(APIView):
    """Dashboard view (class-based version)"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        
        # Check if user has a company
        if hasattr(user, 'company') and user.company:
            # User belongs to a company - filter data by company
            company = user.company
            
            if user.role == 'admin' or user.is_superuser:
                # Company admin sees company-specific data
                stats = {
                    'total_users': User.objects.filter(company=company).count(),
                    'total_customers': User.objects.filter(company=company, role='customer').count(),
                    'total_staff': User.objects.filter(
                        company=company,
                        role__in=['admin', 'staff', 'technician', 'accountant', 'support']
                    ).count(),
                    'company_info': {
                        'name': company.name,
                        'total_customers': company.total_customers,
                        'active_customers': company.active_customers,
                    },
                    'recent_activity': list(AuditLog.objects.filter(
                        tenant=user.tenant
                    ).order_by('-timestamp')[:10].values(
                        'id', 'user__email', 'action', 'model_name', 'object_repr', 'timestamp'
                    )),
                }
            elif user.role == 'staff':
                # Staff sees limited company data
                stats = {
                    'total_customers': User.objects.filter(company=company, role='customer').count(),
                    'company_info': {
                        'name': company.name,
                    },
                    'recent_activity': list(AuditLog.objects.filter(
                        tenant=user.tenant
                    ).order_by('-timestamp')[:10].values(
                        'id', 'user__email', 'action', 'model_name', 'object_repr', 'timestamp'
                    )),
                }
            else:
                # Customer sees only their info
                stats = {
                    'user_info': ProfileSerializer(user).data,
                    'company_info': {
                        'name': company.name,
                    },
                }
        else:
            # Superuser or user without company (global view)
            if user.is_superuser:
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
            else:
                # Regular user without company assignment
                stats = {
                    'user_info': ProfileSerializer(user).data,
                    'warning': 'No company assigned. Please contact administrator.',
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

class CompanyRegisterView(generics.CreateAPIView):
    """Public endpoint to register a new ISP/company + first admin user"""
    permission_classes = [AllowAny]
    serializer_class = CompanyRegisterSerializer
    
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        
        # Create company
        company = Company.objects.create(
            name=data['company_name'],
            email=data['company_email'],
            phone_number=data.get('company_phone', ''),
            address=data.get('company_address', ''),
            city=data.get('company_city', ''),
            county=data.get('company_county', ''),
            registration_number=data.get('company_registration_number', ''),
            tax_pin=data.get('company_tax_pin', ''),
            website=data.get('company_website', ''),
            company_type='isp',
            subscription_plan='basic',
            is_active=True
        )
        
        # Prevent duplicate empty registration_number
        if not company.registration_number.strip():  # if empty or whitespace
            from uuid import uuid4
            company.registration_number = f"REG-{uuid4().hex[:10].upper()}"
            company.save()

        # Auto-generate slug if not set
        if not company.slug:
            from django.utils.text import slugify
            base_slug = slugify(company.name) or 'company'  # fallback if name empty
            slug = base_slug
            counter = 1
            while Company.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            company.slug = slug
            company.save()
        
        # Create Tenant with 14-day trial
        trial_end = timezone.now() + timedelta(days=14)
        tenant = Tenant.objects.create(
            company=company,
            subdomain=company.slug,  # Use company slug as default subdomain
            database_name=f"isp_{company.slug.replace('-', '_')}",  # Optional
            status='trial',
            max_users=10,
            max_customers=100,
            features={},  # Or default features dict
            billing_cycle='monthly',
            monthly_rate=0.00,
            next_billing_date=trial_end.date(),
            subscription_expiry=trial_end.date()
        )
        
        # Create Domain mapping for subdomain (required for django-tenants)
        domain_name = f"{tenant.subdomain}.localhost"  # Dev example
        # In production: f"{tenant.subdomain}.{settings.MAIN_DOMAIN}" e.g. bluenet.example.com
        
        Domain.objects.create(
            domain=domain_name,
            tenant=tenant,
            is_primary=True
        )

        # Run migrations for the new tenant schema
        # This creates the schema and all tables for this tenant
        try:
            call_command('migrate_schemas', schema_name=tenant.schema_name, interactive=False, verbosity=0)
        except Exception as e:
            # Log the error but continue - schema might already exist
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Error running migrations for tenant {tenant.schema_name}: {e}")

        # Create first admin user in the TENANT schema (not public)
        # This ensures the user can authenticate when accessing via subdomain
        # NOTE: Don't set company/tenant FKs - those tables are in public schema only
        with tenant_context(tenant):
            tenant_user = User.objects.create(
                email=data['admin_email'],
                first_name=data['admin_first_name'],
                last_name=data['admin_last_name'],
                phone_number=data['admin_phone'],
                role='admin',
                # company and tenant are NOT set here - they don't exist in tenant schema
                is_active=True,
                is_staff=True,  # Allow admin access
                is_superuser=True,  # First user is superuser
                is_verified=False  # Or True if you skip verification
            )
            tenant_user.set_password(data['admin_password'])
            tenant_user.save()
        
        # Also create user in PUBLIC schema for cross-schema reference
        user = User.objects.create(
            email=data['admin_email'],
            first_name=data['admin_first_name'],
            last_name=data['admin_last_name'],
            phone_number=data['admin_phone'],
            role='admin',
            company=company,
            tenant=tenant,
            is_active=True,
            is_staff=True,  # Allow admin access
            is_verified=False  # Or True if you skip verification
        )
        user.set_password(data['admin_password'])
        user.save()
        
        # Generate JWT tokens (use the public user for now)
        refresh = RefreshToken.for_user(user)
        
        # Log the creation
        AuditLog.log_action(
            user=user,
            action='create',
            model_name='Company',
            object_id=str(company.id),
            object_repr=company.name,
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
            tenant=tenant
        )
        
        # Send email with subdomain, username, password
        self.send_welcome_email(user, tenant, domain_name, data['admin_password'])

        return Response({
            'message': 'Company and admin account created successfully. You are now logged in.',
            'user': {
                'id': user.id,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'role': user.role,
                'company': {
                    'id': company.id,
                    'name': company.name,
                    'email': company.email
                }
            },
            'access': str(refresh.access_token),
            'refresh': str(refresh),
        }, status=status.HTTP_201_CREATED)

    def send_welcome_email(self, user, tenant, domain_name, password):
        """Send welcome email with subdomain and credentials"""
        subject = f"Welcome to {tenant.company.name} - Your Account Details"
        context = {
            'user': user,
            'company': tenant.company,
            'subdomain_url': f"http://{domain_name}:8000/",  # Dev - in production: https://{domain_name}/
            'username': user.email,
            'password': password,  # Note: Sending plain password is insecure - consider reset link instead
            'expiry': tenant.subscription_expiry,
        }
        
        # Render HTML message from template (create this file later)
        html_message = render_to_string('emails/welcome_email.html', context)
        plain_message = strip_tags(html_message)
        
        # Send email
        send_mail(
            subject,
            plain_message,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            html_message=html_message,
            fail_silently=False,
        )