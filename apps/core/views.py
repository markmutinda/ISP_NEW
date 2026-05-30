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
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from datetime import timedelta
from django_tenants.utils import schema_context, get_public_schema_name  # Add this import
from .models import GlobalSystemSettings  # Add this
from .serializers import GlobalSystemSettingsSerializer, CustomTokenRefreshSerializer  # Add this
from rest_framework_simplejwt.exceptions import InvalidToken  # Already needed for token fix
from .serializers import CompanyRegisterSerializer
from django.template.loader import render_to_string  # For email template
from django.utils.html import strip_tags  # For plain text email
from .models import Domain   # ← This is your custom Domain in core/models.
import logging
from django.shortcuts import get_object_or_404  # Add this import
from .otp_service import OTPService, OTPError, OTPRateLimitedError
from .email_delivery import send_transactional_email

from .models import User, Company, SystemSettings, AuditLog, Tenant, Changelog, FeatureRequest, FeatureUpvote  # Add FeatureRequest and FeatureUpvote here
from .serializers import (
    CustomTokenRefreshSerializer, UserSerializer, LoginSerializer, UserCreateSerializer, UserUpdateSerializer,
    ProfileSerializer, PasswordChangeSerializer,
    CompanySerializer, TenantSerializer, SystemSettingsSerializer, AuditLogSerializer,
    CustomTokenObtainPairSerializer, DashboardStatsSerializer, ChangelogSerializer,
    FeatureRequestSerializer, CompanyBrandingSerializer  # Add FeatureRequestSerializer here
)
from .permissions import IsAdmin, IsAdminOrStaff, IsCustomer, IsTechnician

logger = logging.getLogger(__name__)


def _platform_admin_emails() -> set[str]:
    configured = getattr(settings, "OTP_EXEMPT_EMAILS", []) or []
    emails = {str(e).strip().lower() for e in configured if str(e).strip()}
    emails.add("admin@netily.co.ke")
    return emails


def _resolve_cross_tenant_platform_admin(request, email: str, password: str):
    """
    Resolve platform admin credentials from public schema and mirror them into
    the active tenant schema so JWT user_id remains tenant-valid.
    """
    if not email or not password:
        return None
    normalized_email = email.strip().lower()
    if normalized_email not in _platform_admin_emails():
        return None
    try:
        with schema_context(get_public_schema_name()):
            public_user = User.objects.filter(email=normalized_email, is_active=True).first()
            if not public_user or not public_user.check_password(password):
                return None

            tenant_user = User.objects.filter(email=normalized_email).first()
            if not tenant_user:
                tenant_scope = getattr(getattr(request, "tenant", None), "subdomain", "") or ""
                phone_seed = "".join(ch for ch in (tenant_scope or "0") if ch.isdigit())[:6]
                if not phone_seed:
                    phone_seed = str((abs(hash(tenant_scope or "tenant")) % 900000) + 100000)
                tenant_user = User.objects.create(
                    email=normalized_email,
                    first_name=public_user.first_name or "Netily",
                    last_name=public_user.last_name or "Admin",
                    phone_number=f"+254700{phone_seed}",
                    role="admin",
                    is_active=True,
                    is_staff=True,
                    is_superuser=True,
                    is_verified=True,
                    company_name=getattr(getattr(request, "company", None), "name", "") or "",
                    tenant_subdomain=getattr(getattr(request, "tenant", None), "subdomain", "") or "",
                )
            else:
                tenant_user.is_active = True
                tenant_user.is_staff = True
                tenant_user.is_superuser = True
                tenant_user.role = "admin"
                if not tenant_user.first_name:
                    tenant_user.first_name = public_user.first_name or "Netily"
                if not tenant_user.last_name:
                    tenant_user.last_name = public_user.last_name or "Admin"
            tenant_user.password = public_user.password
            tenant_user.save()
            return tenant_user
    except Exception:
        logger.exception("Failed to resolve tenant-local platform admin for %s", normalized_email)
        return None


def get_current_public_company_and_tenant(request):
    """
    Resolve the current tenant's public Company/Tenant records.

    Tenant-schema users deliberately keep company/tenant FKs null because those
    FKs cannot safely point across schemas. Middleware attaches request.tenant
    and request.company from the public schema, and this helper re-queries those
    records in the public schema before reads or writes.
    """
    user = getattr(request, 'user', None)
    request_tenant = getattr(request, 'tenant', None)
    request_company = getattr(request, 'company', None)

    tenant_id = getattr(request_tenant, 'id', None)
    tenant_subdomain = (
        getattr(request_tenant, 'subdomain', None)
        or getattr(user, 'tenant_subdomain', None)
    )
    company_id = getattr(request_company, 'id', None) or getattr(getattr(user, 'company', None), 'id', None)
    company_name = getattr(request_company, 'name', None) or getattr(user, 'company_name', None)

    with schema_context(get_public_schema_name()):
        tenant = None
        company = None

        if tenant_id:
            tenant = Tenant.objects.select_related('company').filter(id=tenant_id).first()
        if not tenant and tenant_subdomain:
            tenant = Tenant.objects.select_related('company').filter(subdomain=tenant_subdomain).first()

        if tenant:
            company = tenant.company
        elif company_id:
            company = Company.objects.filter(id=company_id).first()
        elif company_name:
            company = Company.objects.filter(name=company_name).first()

        return company, tenant


class DebugAuthView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        # Debug information
        debug_info = {
            'user': {
                'id': request.user.id,
                'email': request.user.email,
                'is_authenticated': request.user.is_authenticated,
                'is_superuser': request.user.is_superuser,
                'tenant_subdomain': getattr(request.user, 'tenant_subdomain', None),
                'company_name': getattr(request.user, 'company_name', None),
            },
            'request': {
                'has_tenant': hasattr(request, 'tenant'),
                'has_company': hasattr(request, 'company'),
                'tenant_subdomain': getattr(request.tenant, 'subdomain', None) if hasattr(request, 'tenant') else None,
                'company_name': getattr(request.company, 'name', None) if hasattr(request, 'company') else None,
            },
            'auth_header': request.META.get('HTTP_AUTHORIZATION', 'None'),
            'path': request.path,
        }
        
        logger.debug(f"DebugAuthView: {debug_info}")
        return Response(debug_info)
        
class CustomTokenObtainPairView(TokenObtainPairView):
    """Custom JWT token view with additional user data"""
    serializer_class = CustomTokenObtainPairSerializer

    def post(self, request, *args, **kwargs):
        email = (request.data.get("email") or "").strip().lower()
        password = request.data.get("password") or ""
        otp_code = (request.data.get("otp_code") or "").strip()
        challenge_id = request.data.get("challenge_id") or request.data.get("otp_id")

        if not email or not password:
            return Response({"detail": "Email and password are required."}, status=status.HTTP_400_BAD_REQUEST)

        exempt_emails = _platform_admin_emails()

        # Always prioritize platform-admin resolution on tenant schemas to avoid
        # matching a local customer account with the same email.
        is_platform_admin_email = email in exempt_emails
        user = _resolve_cross_tenant_platform_admin(request, email, password)
        if not user:
            # Critical safety rule:
            # if email belongs to platform-admin list and platform resolution fails,
            # do NOT fall back to tenant-local authenticate() to avoid accidental
            # login as a customer/support account with same email.
            if is_platform_admin_email:
                return Response({"detail": "Invalid platform admin credentials."}, status=status.HTTP_401_UNAUTHORIZED)
            user = authenticate(request=request, username=email, password=password)

        if not user:
            return Response({"detail": "Invalid email or password."}, status=status.HTTP_401_UNAUTHORIZED)
        if not user.is_active:
            return Response({"detail": "Account is disabled."}, status=status.HTTP_403_FORBIDDEN)
        if OTPService.is_otp_exempt_user(user):
            requires_otp = False
        else:
            tenant_schema = getattr(getattr(request, "tenant", None), "schema_name", None)
            if tenant_schema and tenant_schema != "public":
                requires_otp = bool(GlobalSystemSettings.get_solo().admin_email_otp_enabled)
            else:
                requires_otp = False

        tenant_scope = getattr(getattr(request, "tenant", None), "schema_name", "") or "public"
        session_scope = (
            request.headers.get("X-Session-ID")
            or request.COOKIES.get("sessionid")
            or request.session.session_key
            or f"{request.META.get('REMOTE_ADDR', '')}:{request.META.get('HTTP_USER_AGENT', '')[:40]}"
        )

        if requires_otp and not (challenge_id and otp_code):
            if not user.email:
                return Response({"detail": "This account has no email for OTP delivery."}, status=status.HTTP_400_BAD_REQUEST)
            try:
                challenge = OTPService.start_login_challenge(
                    user=user,
                    tenant_scope=tenant_scope,
                    session_scope=session_scope,
                    ip_address=request.META.get("REMOTE_ADDR", ""),
                )
            except OTPRateLimitedError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
            except Exception:
                logger.exception("Failed to issue login OTP for user_id=%s", user.id)
                return Response({"detail": "Failed to send OTP email. Please try again."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            email_parts = user.email.split("@")
            masked_email = f"{email_parts[0][:2]}***@{email_parts[1]}" if len(email_parts) == 2 else "***"
            return Response({
                "requires_otp": True,
                "challenge_id": str(challenge.id),
                "email": masked_email,
                "message": "OTP sent to your registered email.",
                "expires_in": int((challenge.expires_at - timezone.now()).total_seconds()),
                "resend_available_in": int(getattr(settings, "OTP_RESEND_COOLDOWN_SECONDS", 60)),
                "max_resends": int(getattr(settings, "OTP_LOGIN_MAX_RESENDS", 5)),
            }, status=status.HTTP_202_ACCEPTED)

        if requires_otp:
            try:
                OTPService.verify_login_challenge(
                    user=user,
                    challenge_id=challenge_id,
                    code=otp_code,
                    tenant_scope=tenant_scope,
                    session_scope=session_scope,
                )
            except OTPError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        refresh = RefreshToken.for_user(user)
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        return Response({
            "refresh": str(refresh),
            "access": str(refresh.access_token),
            "user": {
                "id": user.id,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "role": user.role,
                "is_verified": user.is_verified,
                "is_staff": user.is_staff,
                "is_superuser": user.is_superuser,
            },
        })


class ResendLoginOTPView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        password = request.data.get("password") or ""
        challenge_id = request.data.get("challenge_id")

        if not email or not password or not challenge_id:
            return Response({"detail": "Email, password and challenge_id are required."}, status=status.HTTP_400_BAD_REQUEST)

        exempt_emails = _platform_admin_emails()
        user = _resolve_cross_tenant_platform_admin(request, email, password)
        if not user:
            if email in exempt_emails:
                return Response({"detail": "Invalid platform admin credentials."}, status=status.HTTP_401_UNAUTHORIZED)
            user = authenticate(request=request, username=email, password=password)
        if not user:
            return Response({"detail": "Invalid email or password."}, status=status.HTTP_401_UNAUTHORIZED)
        if not user.is_active:
            return Response({"detail": "Account is disabled."}, status=status.HTTP_403_FORBIDDEN)

        tenant_scope = getattr(getattr(request, "tenant", None), "schema_name", "") or "public"
        session_scope = (
            request.headers.get("X-Session-ID")
            or request.COOKIES.get("sessionid")
            or request.session.session_key
            or f"{request.META.get('REMOTE_ADDR', '')}:{request.META.get('HTTP_USER_AGENT', '')[:40]}"
        )

        try:
            challenge = OTPService.resend_login_challenge(
                user=user,
                challenge_id=challenge_id,
                tenant_scope=tenant_scope,
                session_scope=session_scope,
                ip_address=request.META.get("REMOTE_ADDR", ""),
            )
        except OTPRateLimitedError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        except OTPError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception("Failed to resend login OTP for user_id=%s", user.id)
            return Response({"detail": "Failed to resend OTP. Please try again."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        email_parts = user.email.split("@")
        masked_email = f"{email_parts[0][:2]}***@{email_parts[1]}" if len(email_parts) == 2 else "***"
        return Response({
            "requires_otp": True,
            "challenge_id": str(challenge.id),
            "email": masked_email,
            "message": "A new OTP has been sent.",
            "expires_in": int((challenge.expires_at - timezone.now()).total_seconds()),
            "resend_available_in": int(getattr(settings, "OTP_RESEND_COOLDOWN_SECONDS", 60)),
            "resend_count": int(getattr(challenge, "resend_count", 0)),
            "max_resends": int(getattr(settings, "OTP_LOGIN_MAX_RESENDS", 5)),
        }, status=status.HTTP_200_OK)

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
        Automatically determine staff status and company context based on the assigned role.
        """
        # 1. Get the role from the request
        role = self.request.data.get('role')
        
        # 2. Define roles that should automatically have dashboard (staff) access
        staff_roles = ['admin', 'support', 'technician', 'accountant', 'staff']
        is_staff_status = role in staff_roles
        
        # 3. Prepare the save arguments
        # We auto-verify staff so they don't get stuck at a 'Verify Email' screen
        save_kwargs = {
            'is_staff': is_staff_status,
            'is_verified': True if is_staff_status else False
        }
        
        # 4. Handle Company assignment (if not a superuser)
        if not self.request.user.is_superuser:
            if hasattr(self.request.user, 'company') and self.request.user.company:
                save_kwargs['company'] = self.request.user.company
        
        # 5. Save the user with these calculated flags
        serializer.save(**save_kwargs)
        
        logger.info(f"UserViewSet: Created {role} user {serializer.instance.email}. is_staff={is_staff_status}")
    
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
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    
    def get_permissions(self):
        if self.action in ['create', 'destroy']:
            permission_classes = [IsAuthenticated, IsAdmin]
        else:
            permission_classes = self.permission_classes
        return [permission() for permission in permission_classes]

    def _get_current_company(self):
        company, _tenant = get_current_public_company_and_tenant(self.request)
        return company

    def get_queryset(self):
        queryset = super().get_queryset()

        if self.request.user.is_superuser:
            return queryset

        company = self._get_current_company()
        if company:
            return queryset.filter(id=company.id)

        return queryset.none()

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    @action(detail=False, methods=['get', 'patch'], url_path='current')
    def current(self, request):
        company, tenant = get_current_public_company_and_tenant(request)
        if not company:
            return Response(
                {'error': 'Company not found. Please contact support.'},
                status=status.HTTP_404_NOT_FOUND
            )

        if request.method.lower() == 'get':
            serializer = self.get_serializer(company, context={'request': request})
            return Response(serializer.data)

        old_name = company.name
        with schema_context(get_public_schema_name()):
            company = Company.objects.get(id=company.id)
            serializer = self.get_serializer(
                company,
                data=request.data,
                partial=True,
                context={'request': request}
            )
            serializer.is_valid(raise_exception=True)
            company = serializer.save(updated_by=request.user)

        if tenant and old_name != company.name:
            with schema_context(tenant.schema_name):
                User.objects.filter(company_name=old_name).update(company_name=company.name)

        return Response(self.get_serializer(company, context={'request': request}).data)


class TenantBrandingView(APIView):
    """
    Current tenant/company branding for dashboard headers.

    GET is available to any authenticated dashboard user. PATCH is restricted to
    tenant admins and updates the public Company record created at registration.
    """
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_permissions(self):
        if self.request.method.lower() in ['patch', 'put']:
            return [IsAuthenticated(), IsAdmin()]
        return [IsAuthenticated()]

    def _response_data(self, company, tenant, request):
        serializer = CompanyBrandingSerializer(company, context={'request': request})
        data = serializer.data
        data['tenant_subdomain'] = getattr(tenant, 'subdomain', None)
        data['tenant_domain'] = getattr(tenant, 'domain', None)
        return data

    def get(self, request):
        company, tenant = get_current_public_company_and_tenant(request)
        if not company:
            return Response(
                {'error': 'Company branding is not available for this account.'},
                status=status.HTTP_404_NOT_FOUND
            )
        return Response(self._response_data(company, tenant, request))

    def patch(self, request):
        company, tenant = get_current_public_company_and_tenant(request)
        if not company:
            return Response(
                {'error': 'Company branding is not available for this account.'},
                status=status.HTTP_404_NOT_FOUND
            )

        old_name = company.name
        with schema_context(get_public_schema_name()):
            company = Company.objects.get(id=company.id)
            serializer = CompanyBrandingSerializer(
                company,
                data=request.data,
                partial=True,
                context={'request': request}
            )
            serializer.is_valid(raise_exception=True)
            company = serializer.save()

        if tenant and old_name != company.name:
            with schema_context(tenant.schema_name):
                User.objects.filter(company_name=old_name).update(company_name=company.name)

        return Response(self._response_data(company, tenant, request))

    def put(self, request):
        return self.patch(request)


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
        import logging as _logging
        import traceback as _traceback
        _log = _logging.getLogger(__name__)
        request_id = request.headers.get("X-Request-ID", "")

        _log.info(
            "Company registration request received request_id=%s remote_addr=%s",
            request_id or "-",
            request.META.get("REMOTE_ADDR", "-"),
        )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            return self._create_company(request, data, _log)
        except Exception as exc:
            _log.error(
                "Company registration failed for '%s':\n%s",
                data.get('company_name', '?'),
                _traceback.format_exc(),
            )
            # Clean up any partial records so re-registration works
            try:
                from django.db import connection as _conn
                _conn.set_schema_to_public()
            except Exception:
                pass
            self._cleanup_registration_artifacts(
                company_name=data.get("company_name"),
                admin_email=data.get("admin_email"),
            )
            return Response(
                {'error': 'Registration failed', 'detail': str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def _create_company(self, request, data, _log):
        # Start in public schema
        from django.db import connection
        connection.set_schema_to_public()
        request_id = request.headers.get("X-Request-ID", "")
        company = None
        tenant = None
        
        # 1. Generate Slug BEFORE creating the object
        from django.utils.text import slugify
        slug = slugify(data['company_name']) or 'company'
        
        # 2. Ensure Slug Uniqueness (Handle duplicates like "Blue Net" vs "Blue Net")
        original_slug = slug
        counter = 1
        while Company.objects.filter(slug=slug).exists():
            slug = f"{original_slug}-{counter}"
            counter += 1

        # 3. Create company in public schema (With the slug!)
        try:
            company = Company.objects.create(
                name=data['company_name'],
                slug=slug,  # Use the generated unique slug
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
            
            # Create Tenant in public schema
            trial_end = timezone.now() + timedelta(days=14)
            schema_name = f"tenant_{company.slug.replace('-', '_')}"
            if self._schema_exists(schema_name):
                raise RuntimeError(
                    f"Registration cannot continue because schema '{schema_name}' already exists. "
                    "Clean up the orphaned tenant schema first."
                )

            tenant = Tenant.objects.create(
                company=company,
                subdomain=company.slug,
                schema_name=schema_name,
                database_name=f"isp_{company.slug.replace('-', '_')}",
                status='trial',
                max_users=10,
                max_customers=100,
                features={},
                billing_cycle='monthly',
                monthly_rate=0.00,
                next_billing_date=trial_end.date(),
                subscription_expiry=trial_end.date()
            )
            
            # 4. Create Domain in public schema
            # In production use the real base domain (e.g. acme.netily.co.ke),
            # in local dev fall back to subdomain.localhost:8000
            base_domain = getattr(settings, 'TENANT_BASE_DOMAIN', None)
            if base_domain:
                domain_name = f"{tenant.subdomain}.{base_domain}"
                domain_protocol = 'https'
            else:
                domain_name = f"{tenant.subdomain}.localhost:8000" if settings.DEBUG else f"{tenant.subdomain}.localhost"
                domain_protocol = 'http'

            Domain.objects.create(
                domain=domain_name,
                tenant=tenant,
                is_primary=True
            )

            self._create_schema(tenant.schema_name)

            # Run tenant migrations in a subprocess so an OOM kill of the child
            # process does not kill the gunicorn worker and reset the client connection.
            import subprocess, sys, os as _os
            migrate_env = _os.environ.copy()
            migrate_env['DJANGO_SETTINGS_MODULE'] = 'config.settings.production'
            result = subprocess.run(
                [sys.executable, 'manage.py', 'migrate_schemas_resilient',
                 '--schema', tenant.schema_name],
                cwd=settings.BASE_DIR,
                env=migrate_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=240,   # hard cap — gunicorn timeout is 300s
            )
            if result.returncode != 0:
                self._drop_schema_if_exists(tenant.schema_name)
                raise RuntimeError(
                    f"Tenant schema migration failed for {tenant.schema_name}.\n"
                    f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
                )

            try:
                from apps.radius.services.tenant_radius_service import tenant_radius_service

                tenant_radius_service.configure_tenant_radius(
                    schema_name=tenant.schema_name,
                    tenant_name=company.name,
                )
            except Exception:
                logger.exception(
                    "RADIUS provisioning failed after successful tenant migration for %s",
                    tenant.schema_name,
                )

            # Switch to tenant schema
            connection.set_tenant(tenant)
        
            # Create user with all necessary information
            user = User.objects.create(
               email=data['admin_email'],
               first_name=data['admin_first_name'],
               last_name=data['admin_last_name'],
               phone_number=data['admin_phone'],
               role='admin',
               # Foreign keys remain None (can't reference public schema from tenant schema)
               company=None,
               tenant=None,
                # Store denormalized info
                company_name=company.name,
                tenant_subdomain=tenant.subdomain,
                is_active=True,
                is_staff=True,
                is_superuser=True,
                is_verified=True
            )
            user.set_password(data['admin_password'])
            user.save()

            email_result = self.send_welcome_email(
                user=user,
                tenant=tenant,
                domain_name=domain_name,
                password=data['admin_password'],
            )
        except Exception:
            connection.set_schema_to_public()
            self._cleanup_registration_artifacts(
                company_name=data.get("company_name"),
                admin_email=data.get("admin_email"),
                company_id=getattr(company, "id", None),
                tenant_id=getattr(tenant, "id", None),
                schema_name=getattr(tenant, "schema_name", None),
            )
            raise
    
        # Removed sensitive debug print line
        
        # Switch back to public schema
        connection.set_schema_to_public()
        
        # Generate tokens
        refresh = RefreshToken.for_user(user)
        
        response_payload = {
            'message': 'Company created successfully',
            'company': company.name,
            'tenant': tenant.subdomain,
            'subdomain': tenant.subdomain,       # alias expected by frontend
            'tenant_domain': domain_name,
            'login_url': f'{domain_protocol}://{domain_name}/admin/login/',
            'dashboard_url': f'{domain_protocol}://{domain_name}/admin/',
            'email': user.email,
            'access': str(refresh.access_token),
            # 'user' object expected by frontend for token storage
            'user': {
                'id': user.id,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'role': user.role,
                'company': {
                    'name': company.name,
                    'slug': company.slug,
                    'subdomain': tenant.subdomain,
                },
            },
            'welcome_email': email_result,
        }
        if not email_result.get('sent'):
            response_payload['warnings'] = [
                'Tenant was created, but the welcome email was not delivered.'
            ]

        _log.info(
            "Company registration completed request_id=%s company=%s tenant=%s welcome_email_sent=%s",
            request_id or "-",
            company.name,
            tenant.subdomain,
            email_result.get("sent"),
        )

        return Response(response_payload, status=status.HTTP_201_CREATED)

    def _cleanup_registration_artifacts(
        self,
        *,
        company_name=None,
        admin_email=None,
        company_id=None,
        tenant_id=None,
        schema_name=None,
    ):
        from django.utils.text import slugify

        connection.set_schema_to_public()

        derived_schema = schema_name
        if not derived_schema and company_name:
            derived_schema = f"tenant_{slugify(company_name).replace('-', '_')}"

        if derived_schema:
            try:
                self._drop_schema_if_exists(derived_schema)
            except Exception:
                logger.exception("Failed dropping schema during registration cleanup: %s", derived_schema)

        if tenant_id:
            try:
                Tenant.objects.filter(pk=tenant_id).delete()
            except Exception:
                logger.exception("Failed deleting tenant id=%s during registration cleanup", tenant_id)
        elif derived_schema:
            try:
                Tenant.objects.filter(schema_name=derived_schema).delete()
            except Exception:
                logger.exception("Failed deleting tenant schema=%s during registration cleanup", derived_schema)

        if company_id:
            try:
                Company.objects.filter(pk=company_id).delete()
            except Exception:
                logger.exception("Failed deleting company id=%s during registration cleanup", company_id)
        elif company_name:
            try:
                Company.objects.filter(name__iexact=company_name).delete()
            except Exception:
                logger.exception("Failed deleting company name=%s during registration cleanup", company_name)

        if admin_email:
            try:
                User.objects.filter(email=admin_email).delete()
            except Exception:
                logger.exception("Failed deleting user email=%s during registration cleanup", admin_email)

    def _schema_exists(self, schema_name):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.schemata
                    WHERE schema_name = %s
                )
                """,
                [schema_name],
            )
            return cursor.fetchone()[0]

    def _create_schema(self, schema_name):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema_name}"')

    def _drop_schema_if_exists(self, schema_name):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute('SET search_path TO "public"')
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')

    def send_welcome_email(self, user, tenant, domain_name, password):
        """Send welcome email with subdomain and credentials"""
        subject = f"Welcome to {tenant.company.name} on Netily - Your Login Details"
        context = {
            'user': user,
            'company': tenant.company,
            'subdomain_url': f"https://{domain_name}/admin/login/",
            'username': user.email,
            'password': password,  # Note: Sending plain password is insecure - consider reset link instead
            'expiry': tenant.subscription_expiry,
        }
        
        # Render HTML message from template (create this file later)
        html_message = render_to_string('emails/welcome_email.html', context)
        plain_message = strip_tags(html_message)
        
        result = send_transactional_email(
            subject=subject,
            recipient=user.email,
            plain_message=plain_message,
            html_message=html_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
        )
        if not result.get('sent'):
            logger.error(
                "Welcome email failed for tenant registration %s: %s",
                tenant.subdomain,
                result.get('error'),
            )
        return result


class CompanyRegistrationStatusView(APIView):
    """Confirm whether a public registration request completed successfully."""
    permission_classes = [AllowAny]

    def get(self, request):
        from django.db import connection

        connection.set_schema_to_public()

        company_name = (request.query_params.get("company_name") or "").strip()
        company_email = (request.query_params.get("company_email") or "").strip()

        if not company_name or not company_email:
            return Response(
                {"detail": "company_name and company_email are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tenant = (
            Tenant.objects.select_related("company")
            .filter(
                company__name__iexact=company_name,
                company__email__iexact=company_email,
            )
            .order_by("-created_at")
            .first()
        )

        if not tenant or not tenant.company:
            return Response(
                {"ready": False, "detail": "Registration is still being finalized."},
                status=status.HTTP_404_NOT_FOUND,
            )

        base_domain = getattr(settings, "TENANT_BASE_DOMAIN", None)
        if base_domain:
            domain_name = f"{tenant.subdomain}.{base_domain}"
            domain_protocol = "https"
        else:
            domain_name = (
                f"{tenant.subdomain}.localhost:8000"
                if settings.DEBUG
                else f"{tenant.subdomain}.localhost"
            )
            domain_protocol = "http"

        return Response(
            {
                "ready": True,
                "company": tenant.company.name,
                "subdomain": tenant.subdomain,
                "tenant_domain": domain_name,
                "login_url": f"{domain_protocol}://{domain_name}/admin/login/",
                "dashboard_url": f"{domain_protocol}://{domain_name}/admin/",
            },
            status=status.HTTP_200_OK,
        )


class PlatformChangelogView(APIView):
    """
    Read-only view for ISPs to see platform updates.
    Safely reads from the public schema while the user is logged into a tenant.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Temporarily switch to the public schema to read the global changelogs
        with schema_context(get_public_schema_name()):
            # Only fetch published changelogs
            changelogs = Changelog.objects.filter(is_published=True)
            # Evaluate the queryset immediately inside the public context using list()
            serializer = ChangelogSerializer(list(changelogs), many=True)
            return Response(serializer.data)


class CommunityFeatureRequestView(APIView):
    """
    Community feature requests board.
    Allows ISPs to view, create, and vote on feature requests.
    Lives in public schema so all ISPs can participate.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Get all feature requests with upvote status for current ISP"""
        with schema_context(get_public_schema_name()):
            requests = FeatureRequest.objects.all()
            serializer = FeatureRequestSerializer(requests, many=True, context={'request': request})
            return Response(serializer.data)

    def post(self, request):
        """Create a new feature request as the current ISP"""
        with schema_context(get_public_schema_name()):
            # Assign the request to the current ISP (tenant)
            serializer = FeatureRequestSerializer(data=request.data, context={'request': request})
            if serializer.is_valid():
                serializer.save(requested_by_tenant=request.tenant)
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ToggleUpvoteView(APIView):
    """
    Toggle upvote on a feature request.
    One ISP = one vote per feature.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        """Toggle upvote status for a feature request"""
        with schema_context(get_public_schema_name()):
            feature = get_object_or_404(FeatureRequest, pk=pk)
            upvote_qs = FeatureUpvote.objects.filter(feature_request=feature, tenant=request.tenant)
            
            if upvote_qs.exists():
                upvote_qs.delete()
                feature.upvotes_count -= 1
                action = "removed"
            else:
                FeatureUpvote.objects.create(feature_request=feature, tenant=request.tenant)
                feature.upvotes_count += 1
                action = "added"
            
            feature.save()
            return Response({
                "status": "success", 
                "action": action, 
                "count": feature.upvotes_count
            })


class SendOTPView(APIView):
    """
    Send a 6-digit OTP to the authenticated user's email.
    Default purpose is payment-method verification.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if OTPService.is_otp_exempt_user(user):
            return Response({
                "message": "OTP bypassed for this account.",
                "verified": True,
                "email": user.email,
                "bypass": True,
            })

        email = user.email
        if not email:
            return Response({"error": "No email associated with this account."}, status=status.HTTP_400_BAD_REQUEST)

        purpose = request.data.get("purpose") or OTPService.PAYMENT_PURPOSE
        if purpose not in (OTPService.LOGIN_PURPOSE, OTPService.PAYMENT_PURPOSE):
            return Response({"error": "Invalid OTP purpose."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            otp = OTPService.issue_otp(
                user=user,
                purpose=purpose,
                ip_address=request.META.get("REMOTE_ADDR", ""),
            )
        except OTPRateLimitedError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        except Exception as exc:
            logger.error("Failed to send OTP email to %s: %s", email, exc)
            return Response({"error": "Failed to send OTP. Please try again."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        parts = email.split("@")
        masked = parts[0][:2] + "***@" + parts[1] if len(parts) == 2 else "***"

        return Response({
            "message": "OTP sent successfully.",
            "email": masked,
            "otp_id": otp.id,
            "purpose": purpose,
        })


class VerifyOTPView(APIView):
    """
    Verify a 6-digit OTP for the authenticated user.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if OTPService.is_otp_exempt_user(request.user):
            return Response({
                "message": "OTP bypassed for this account.",
                "verified": True,
                "bypass": True,
            })

        otp_code = request.data.get("otp", "").strip()
        otp_id = request.data.get("otp_id")
        purpose = request.data.get("purpose") or OTPService.PAYMENT_PURPOSE

        if not otp_code or len(otp_code) != 6 or not otp_id:
            return Response({"error": "Please provide a valid 6-digit OTP."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            OTPService.verify_and_consume(
                user=request.user,
                otp_id=otp_id,
                code=otp_code,
                purpose=purpose,
            )
        except OTPError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "message": "OTP verified successfully.",
            "verified": True,
        })

class SubmitLeadView(APIView):
    """
    Public endpoint for capturing leads from the landing page.
    No authentication required.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        name = request.data.get("name", "").strip()
        email = request.data.get("email", "").strip()
        phone = request.data.get("phone", "").strip()
        company = request.data.get("company", "").strip()
        lead_source = request.data.get("lead_source", "").strip()
        message = request.data.get("message", "").strip()

        if not name or not email:
            return Response({"error": "Name and email are required."}, status=status.HTTP_400_BAD_REQUEST)

        # Store lead in the public schema
        with schema_context(get_public_schema_name()):
            from .models import Lead
            Lead.objects.create(
                name=name,
                email=email,
                phone=phone,
                company_name=company,
                lead_source=lead_source,
                message=message,
            )

        # Send notification email to admin (in background thread to avoid blocking response)
        import threading
        def _send_lead_email():
            try:
                send_mail(
                    subject=f"New Lead: {name} ({company or 'No company'})",
                    message=(
                        f"New lead submitted:\n\n"
                        f"Name: {name}\n"
                        f"Email: {email}\n"
                        f"Phone: {phone}\n"
                        f"Company: {company}\n"
                        f"Lead Source: {lead_source or 'Not specified'}\n"
                        f"Message: {message}"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[settings.DEFAULT_FROM_EMAIL],
                    fail_silently=True,
                )
            except Exception:
                pass
        threading.Thread(target=_send_lead_email, daemon=True).start()

        return Response({
            "message": "Thank you! We'll be in touch shortly.",
        }, status=status.HTTP_201_CREATED)

