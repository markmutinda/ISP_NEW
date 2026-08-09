"""
Core views for ISP Management System
"""

import json

from rest_framework import viewsets, status, generics, permissions
from rest_framework.response import Response
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView
from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.utils import timezone
from django.conf import settings
from rest_framework_simplejwt.views import TokenRefreshView
from rest_framework.views import APIView
from rest_framework import generics
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from datetime import timedelta
from hashlib import sha1
from django_tenants.utils import schema_context, get_public_schema_name
from .models import GlobalSystemSettings
from .serializers import GlobalSystemSettingsSerializer, CustomTokenRefreshSerializer
from rest_framework_simplejwt.exceptions import InvalidToken
from .serializers import CompanyRegisterSerializer
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from .models import Domain
import logging
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.db import DatabaseError, transaction
from .otp_service import OTPService, OTPError, OTPRateLimitedError
from .email_delivery import send_transactional_email

# Import country/currency constants
from utils.constants import COUNTRY_CURRENCY_MAP

from .models import User, Company, SystemSettings, AuditLog, Tenant, Changelog, FeatureRequest, FeatureUpvote, RoleAccessPolicy
from .rbac_defaults import (
    DEFAULT_ROLE_ACCESS_POLICIES,
    EDITABLE_RBAC_ROLES,
    normalize_role_access_policies,
)
from .serializers import (
    CustomTokenRefreshSerializer, UserSerializer, LoginSerializer, UserCreateSerializer, UserUpdateSerializer,
    AdminUserUpdateSerializer,
    ProfileSerializer, PasswordChangeSerializer,
    CompanySerializer, TenantSerializer, SystemSettingsSerializer, AuditLogSerializer,
    CustomTokenObtainPairSerializer, DashboardStatsSerializer, ChangelogSerializer,
    FeatureRequestSerializer, CompanyBrandingSerializer, RoleAccessPolicySerializer
)
from .permissions import HasRoleAccessPolicy, IsAdmin, IsAdminOrStaff, IsCustomer, IsTechnician
from .session_tokens import issue_refresh_token

logger = logging.getLogger(__name__)


def _platform_admin_emails() -> set[str]:
    configured = getattr(settings, "OTP_EXEMPT_EMAILS", []) or []
    emails = {str(e).strip().lower() for e in configured if str(e).strip()}
    try:
        with schema_context(get_public_schema_name()):
            emails.update(
                str(email).strip().lower()
                for email in User.objects.filter(is_active=True, is_superuser=True)
                .exclude(email__isnull=True)
                .values_list("email", flat=True)
                if str(email).strip()
            )
    except Exception:
        logger.exception("Failed to load active platform superadmin emails")
    return emails


def _tenant_local_platform_admin_exists(email: str) -> bool:
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return False
    try:
        return User.objects.filter(email__iexact=normalized_email, is_superuser=True).exists()
    except Exception:
        return False


def _platform_admin_phone(public_user, tenant_scope: str) -> str:
    tenant_scope = tenant_scope or "tenant"
    tenant_seed = int(sha1(tenant_scope.encode("utf-8")).hexdigest()[:8], 16) % 1_000_000
    user_seed = int(getattr(public_user, "id", 0) or 0) % 1_000
    return f"+2547{user_seed:03d}{tenant_seed:06d}"


def _resolve_cross_tenant_platform_admin(request, email: str, password: str):
    """
    Resolve platform admin credentials from public schema and mirror them into
    the active tenant schema so JWT user_id remains tenant-valid.
    """
    if not email or not password:
        return None
    normalized_email = email.strip().lower()
    try:
        with schema_context(get_public_schema_name()):
            public_user = User.objects.filter(
                email__iexact=normalized_email,
                is_superuser=True,
            ).first()
            if not public_user or not public_user.check_password(password):
                return None

        tenant_scope = getattr(getattr(request, "tenant", None), "subdomain", "") or ""
        tenant_user = User.objects.filter(email__iexact=normalized_email).first()
        if not tenant_user:
            if not public_user.is_active:
                return None

            phone_number = _platform_admin_phone(public_user, tenant_scope)
            collision_counter = 1
            while User.objects.filter(phone_number=phone_number).exists():
                phone_number = _platform_admin_phone(public_user, f"{tenant_scope}-{collision_counter}")
                collision_counter += 1
            tenant_user = User.objects.create(
                email=normalized_email,
                first_name=public_user.first_name or "Netily",
                last_name=public_user.last_name or "Admin",
                phone_number=phone_number,
                role="admin",
                is_active=public_user.is_active,
                is_staff=True,
                is_superuser=True,
                is_verified=True,
                company_name=getattr(getattr(request, "company", None), "name", "") or "",
                tenant_subdomain=getattr(getattr(request, "tenant", None), "subdomain", "") or "",
            )
        else:
            tenant_user.is_active = public_user.is_active
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

        # ============================================================
        # FIX: Reject login attempts on a subdomain that didn't resolve 
        # to a real tenant
        # ============================================================
        if getattr(request, "tenant_not_found", False):
            return Response(
                {"detail": "No account found for this address. Please check the URL and try again."},
                status=status.HTTP_404_NOT_FOUND,
            )

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
        tenant = getattr(request, "tenant", None)
        if (
            tenant
            and getattr(tenant, "schema_name", None) != "public"
            and getattr(tenant, "status", None) == "suspended"
            and not getattr(user, "is_superuser", False)
        ):
            return Response(
                {
                    "detail": (
                        "This ISP workspace is currently suspended. "
                        "Please contact Netily Support to restore access."
                    ),
                    "code": "TENANT_SUSPENDED",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
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

        refresh = issue_refresh_token(user)
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
        has_tenant_platform_mirror = _tenant_local_platform_admin_exists(email)
        user = _resolve_cross_tenant_platform_admin(request, email, password)
        if not user:
            if email in exempt_emails or has_tenant_platform_mirror:
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


class RegisterView(generics.CreateAPIView):
    """View for user registration"""
    permission_classes = [AllowAny]
    serializer_class = UserCreateSerializer
    
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            
            if not user.company and not user.tenant:
                pass
            
            refresh = issue_refresh_token(user)
            
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
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/users"

    def get_required_rbac_path(self, request):
        if getattr(self, "action", None) in ("me", "update_profile", "change_password"):
            return None

        staff_roles = {"admin", "support", "technician", "accountant", "staff"}
        staff_only = str(request.query_params.get("staff_only", "")).lower() in {"1", "true", "yes"}
        requested_role = str(request.data.get("role", "")).strip().lower() if hasattr(request, "data") else ""
        if staff_only or requested_role in staff_roles:
            return "/admin/staff"

        if getattr(self, "action", None) in ("retrieve", "update", "partial_update", "destroy"):
            pk = self.kwargs.get(self.lookup_url_kwarg or self.lookup_field)
            if pk:
                target = User.objects.filter(pk=pk).only("role", "is_staff").first()
                if target and (target.is_staff or str(target.role or "").lower() in staff_roles):
                    return "/admin/staff"
        return self.required_rbac_path
    
    def get_serializer_class(self):
        if self.action == 'create':
            return UserCreateSerializer
        elif self.action in ['update', 'partial_update']:
            if self.request.user.is_superuser or self.request.user.role == 'admin':
                return AdminUserUpdateSerializer
            return UserUpdateSerializer
        return UserSerializer
    
    def get_permissions(self):
        if self.action in ['create', 'destroy']:
            return [IsAuthenticated(), IsAdmin(), HasRoleAccessPolicy()]
        elif self.action in ['me', 'update_profile', 'change_password']:
            permissions_list = [IsAuthenticated()]
            return permissions_list
        elif self.action in ['update', 'partial_update']:
            return [IsAuthenticated(), IsAdminOrStaff(), HasRoleAccessPolicy()]
        return [IsAuthenticated(), IsAdminOrStaff(), HasRoleAccessPolicy()]
    
    def get_queryset(self):
        qs = super().get_queryset().select_related('company')
        
        if self.request.user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return qs.filter(company_id=company_id)
            return qs

        company, _tenant = get_current_public_company_and_tenant(self.request)
        if company:
            qs = qs.filter(company=company)
        elif hasattr(self.request.user, 'company') and self.request.user.company:
            qs = qs.filter(company=self.request.user.company)
        else:
            return qs.none()

        staff_only = str(self.request.query_params.get("staff_only", "")).lower() in {"1", "true", "yes"}
        if staff_only:
            qs = qs.filter(
                role__in=["admin", "staff", "support", "technician", "accountant"],
                is_staff=True,
            ).exclude(is_superuser=True)

        return qs
    
    def perform_create(self, serializer):
        role = self.request.data.get('role')
        staff_roles = ['admin', 'support', 'technician', 'accountant', 'staff']
        is_staff_status = role in staff_roles
        
        save_kwargs = {
            'is_staff': is_staff_status,
            'is_verified': True if is_staff_status else False
        }
        
        if not self.request.user.is_superuser:
            if hasattr(self.request.user, 'company') and self.request.user.company:
                save_kwargs['company'] = self.request.user.company
        
        serializer.save(**save_kwargs)
        logger.info(f"UserViewSet: Created {role} user {serializer.instance.email}. is_staff={is_staff_status}")

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        """Immediately revoke and remove a tenant staff account."""
        instance = self.get_object()
        if instance.pk == request.user.pk:
            return Response(
                {"detail": "You cannot delete your own signed-in account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if instance.is_superuser:
            return Response(
                {"detail": "Platform administrator accounts cannot be deleted here."},
                status=status.HTTP_403_FORBIDDEN,
            )

        target_id = str(instance.pk)
        target_label = instance.get_full_name() or instance.email or target_id
        target_email = instance.email

        # Make credentials unusable before deletion. The hard delete invalidates
        # every access token through the authentication layer's user lookup, while
        # this step also protects against any in-flight authentication attempt.
        instance.is_active = False
        instance.set_unusable_password()
        instance.save(update_fields=["is_active", "password", "updated_at"])

        AuditLog.log_action(
            user=request.user,
            action="delete",
            model_name="User",
            object_id=target_id,
            object_repr=target_label,
            changes={"email": target_email, "sessions_revoked": True},
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['get', 'patch', 'put'])
    def me(self, request):
        """Get or update current user profile."""
        tenant_subdomain = getattr(request.user, "tenant_subdomain", None)
        company_name = getattr(request.user, "company_name", None)

        if tenant_subdomain or company_name:
            from django_tenants.utils import get_public_schema_name, schema_context
            from apps.core.models import Company as PublicCompany, Tenant as PublicTenant

            with schema_context(get_public_schema_name()):
                tenant_exists = (
                    PublicTenant.objects.filter(subdomain=tenant_subdomain).exists()
                    if tenant_subdomain
                    else False
                )
                company_exists = (
                    PublicCompany.objects.filter(name=company_name).exists()
                    if company_name
                    else False
                )

            if (tenant_subdomain and not tenant_exists) or (company_name and not company_exists):
                logger.warning(
                    "Rejected stale session for deleted tenant account user_id=%s tenant_subdomain=%s company_name=%s",
                    request.user.id,
                    tenant_subdomain,
                    company_name,
                )
                return Response(
                    {"detail": "This tenant account no longer exists."},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            if tenant_subdomain:
                with schema_context(get_public_schema_name()):
                    suspended = PublicTenant.objects.filter(
                        subdomain=tenant_subdomain,
                        status="suspended",
                    ).exists()
                if suspended and not getattr(request.user, "is_superuser", False):
                    return Response(
                        {
                            "detail": (
                                "This ISP workspace is currently suspended. "
                                "Please contact Netily Support to restore access."
                            ),
                            "code": "TENANT_SUSPENDED",
                        },
                        status=status.HTTP_403_FORBIDDEN,
                    )

        if request.method in ("PATCH", "PUT"):
            serializer = UserUpdateSerializer(
                request.user,
                data=request.data,
                partial=request.method == "PATCH",
                context={'request': request},
            )
            if serializer.is_valid():
                serializer.save()
                return Response(ProfileSerializer(request.user).data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

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


class RoleAccessPolicyViewSet(viewsets.ModelViewSet):
    """Manage tenant dashboard route access per staff role."""

    fallback_setting_key = "staff_role_access_policies"

    serializer_class = RoleAccessPolicySerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/staff"
    lookup_field = "role"

    editable_roles = EDITABLE_RBAC_ROLES
    default_paths_by_role = DEFAULT_ROLE_ACCESS_POLICIES

    def get_queryset(self):
        return RoleAccessPolicy.objects.filter(role__in=self.editable_roles)

    def _ensure_defaults(self):
        normalize_role_access_policies()

    def _fallback_map(self):
        raw_value = SystemSettings.get_setting(self.fallback_setting_key, default={}) or {}
        if isinstance(raw_value, dict):
            return {
                role: list(paths) if isinstance(paths, list) else []
                for role, paths in raw_value.items()
                if role in self.editable_roles
            }
        return {}

    def _persist_fallback_map(self, policies: dict[str, list[str]]):
        SystemSettings.objects.update_or_create(
            key=self.fallback_setting_key,
            defaults={
                "name": "Staff role access policies",
                "value": json.dumps(policies),
                "setting_type": "security",
                "data_type": "json",
                "description": "Tenant-scoped route access rules for dashboard staff roles.",
                "is_public": False,
                "updated_by": self.request.user,
            },
        )

    def _build_fallback_payload(self):
        saved_map = self._fallback_map()
        payload = []
        for index, role in enumerate(self.editable_roles, start=1):
            payload.append({
                "id": index,
                "role": role,
                "allowed_paths": saved_map.get(role) or self.default_paths_by_role.get(role, []),
                "created_at": None,
                "updated_at": None,
            })
        return payload

    def list(self, request, *args, **kwargs):
        try:
            self._ensure_defaults()
            queryset = self.get_queryset()
            return Response(RoleAccessPolicySerializer(queryset, many=True).data)
        except DatabaseError:
            return Response(self._build_fallback_payload())

    def get_object(self):
        self._ensure_defaults()
        queryset = self.get_queryset().filter(role=self.kwargs.get(self.lookup_field)).order_by("id")
        instance = queryset.first()
        if not instance:
            raise Http404
        if queryset.count() > 1:
            queryset.exclude(id=instance.id).delete()
        return instance

    def get_permissions(self):
        if self.action in ["update", "partial_update", "create", "destroy"]:
            return [IsAuthenticated(), IsAdmin(), HasRoleAccessPolicy()]
        if self.action == "me":
            # Reading one's own policy is the RBAC bootstrap operation.  It
            # cannot itself require access to /admin/staff, otherwise a user
            # can never discover newly assigned pages after signing in.
            return [IsAuthenticated(), IsAdminOrStaff()]
        return [IsAuthenticated(), IsAdminOrStaff(), HasRoleAccessPolicy()]

    @action(detail=False, methods=["get"], url_path="me")
    def me(self, request):
        """Return only the signed-in user's effective role policy."""
        role = str(getattr(request.user, "role", "") or "").strip().lower().replace("-", "_")
        if role in {"admin", "super_admin", "superadmin"}:
            return Response({
                "role": role,
                "allowed_paths": [],
                "is_unrestricted": True,
                "source": "admin",
            })
        if role not in self.editable_roles:
            return Response(
                {"detail": "No dashboard access policy exists for this role."},
                status=status.HTTP_403_FORBIDDEN,
            )
        custom_allowed = getattr(request.user, "custom_allowed_paths", None)
        if custom_allowed is not None:
            allowed_paths = custom_allowed
            source = "user"
        else:
            try:
                self._ensure_defaults()
                policy = self.get_queryset().filter(role=role).first()
            except DatabaseError:
                policy = None
            allowed_paths = (
                policy.allowed_paths
                if policy is not None
                else self.default_paths_by_role.get(role, [])
            )
            source = "role"
        return Response({
            "role": role,
            "allowed_paths": allowed_paths or [],
            "is_unrestricted": False,
            "source": source,
        })

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def partial_update(self, request, *args, **kwargs):
        role = kwargs.get(self.lookup_field)
        serializer = self.get_serializer(data={
            "role": role,
            "allowed_paths": request.data.get("allowed_paths", []),
        })
        serializer.is_valid(raise_exception=True)

        try:
            instance = self.get_object()
            instance.allowed_paths = serializer.validated_data["allowed_paths"]
            instance.updated_by = request.user
            instance.save(update_fields=["allowed_paths", "updated_by", "updated_at"])
            return Response(RoleAccessPolicySerializer(instance).data)
        except DatabaseError:
            policies = self._fallback_map()
            policies[role] = serializer.validated_data["allowed_paths"]
            self._persist_fallback_map(policies)
            payload = next((item for item in self._build_fallback_payload() if item["role"] == role), None)
            return Response(payload or {
                "id": 0,
                "role": role,
                "allowed_paths": serializer.validated_data["allowed_paths"],
                "created_at": None,
                "updated_at": None,
            })

    def update(self, request, *args, **kwargs):
        return self.partial_update(request, *args, **kwargs)


class CompanyViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Company management
    """
    queryset = Company.objects.all()
    serializer_class = CompanySerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/settings"
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    
    def get_permissions(self):
        if self.action in ['create', 'destroy']:
            permission_classes = [IsAuthenticated, IsAdmin, HasRoleAccessPolicy]
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
    required_rbac_path = "/admin/settings"

    def get_permissions(self):
        if self.request.method.lower() in ['patch', 'put']:
            return [IsAuthenticated(), IsAdmin(), HasRoleAccessPolicy()]
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
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/settings"
    
    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            permission_classes = [IsAuthenticated, IsAdmin, HasRoleAccessPolicy]
        else:
            permission_classes = self.permission_classes
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
        return Response(
            {'message': 'Email verification endpoint. Implement verification logic.'},
            status=status.HTTP_200_OK
        )


class ResendVerificationView(APIView):
    """Resend verification email view"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
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
        user = request.user
        
        if hasattr(user, 'company') and user.company:
            company = user.company
            
            if user.role == 'admin' or user.is_superuser:
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
                stats = {
                    'user_info': ProfileSerializer(user).data,
                    'company_info': {
                        'name': company.name,
                    },
                }
        else:
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
                stats = {
                    'user_info': ProfileSerializer(user).data,
                    'warning': 'No company assigned. Please contact administrator.',
                }
        
        serializer = DashboardStatsSerializer(stats)
        return Response(serializer.data)


@api_view(['POST'])
@permission_classes([AllowAny])
def register_user(request):
    """
    Register a new user (function-based view for compatibility)
    """
    serializer = UserCreateSerializer(data=request.data)
    if serializer.is_valid():
        user = serializer.save()
        
        refresh = issue_refresh_token(user)
        
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
        stats = {
            'total_customers': User.objects.filter(role='customer').count(),
            'recent_activity': AuditLog.objects.all().order_by('-timestamp')[:10].values(
                'id', 'user__email', 'action', 'model_name', 'object_repr', 'timestamp'
            ),
        }
    else:
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
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/logs"
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
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
        from django.db import connection
        connection.set_schema_to_public()
        request_id = request.headers.get("X-Request-ID", "")
        company = None
        tenant = None
        
        from django.utils.text import slugify
        slug = slugify(data['company_name']) or 'company'
        
        original_slug = slug
        counter = 1
        while Company.objects.filter(slug=slug).exists():
            slug = f"{original_slug}-{counter}"
            counter += 1

        # Get country from registration data, default to Kenya
        country = data.get('company_country') or 'KE'
        
        try:
            company = Company.objects.create(
                name=data['company_name'],
                slug=slug,
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
                country=country,
                base_currency=COUNTRY_CURRENCY_MAP.get(country, 'KES'),
                is_active=True
            )
            
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
                timeout=240,
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

            connection.set_tenant(tenant)
        
            user = User.objects.create(
               email=data['admin_email'],
               first_name=data['admin_first_name'],
               last_name=data['admin_last_name'],
               phone_number=data['admin_phone'],
               role='admin',
               company=None,
               tenant=None,
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
    
        connection.set_schema_to_public()

        try:
            from apps.affiliate.services import record_affiliate_signup
            record_affiliate_signup(
                referral_code=data.get("referral_code"),
                attribution_token=data.get("attribution_token"),
                email=data["admin_email"],
                company_name=company.name,
                company=company,
            )
        except Exception:
            logger.exception("Affiliate attribution failed for company %s", company.name)
        
        refresh = issue_refresh_token(user)
        
        response_payload = {
            'message': 'Company created successfully',
            'company': company.name,
            'tenant': tenant.subdomain,
            'subdomain': tenant.subdomain,
            'tenant_domain': domain_name,
            'login_url': f'{domain_protocol}://{domain_name}/admin/login/',
            'dashboard_url': f'{domain_protocol}://{domain_name}/admin/',
            'email': user.email,
            'access': str(refresh.access_token),
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
                    'country': company.country,
                    'base_currency': company.base_currency,
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
            'password': password,
            'expiry': tenant.subscription_expiry,
        }
        
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
        with schema_context(get_public_schema_name()):
            changelogs = Changelog.objects.filter(is_published=True)
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
        with schema_context(get_public_schema_name()):
            requests = FeatureRequest.objects.all()
            serializer = FeatureRequestSerializer(requests, many=True, context={'request': request})
            return Response(serializer.data)

    def post(self, request):
        with schema_context(get_public_schema_name()):
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
    authentication_classes = []
    throttle_scope = "lead_submit"

    def post(self, request):
        text_fields = {
            "name": (200, True),
            "email": (254, True),
            "phone": (20, False),
            "company": (200, False),
            "lead_source": (120, False),
            "referral_name": (200, False),
            "referral_code": (64, False),
            "message": (5000, False),
        }
        cleaned = {}
        for field, (max_length, required) in text_fields.items():
            value = request.data.get(field, "")
            if not isinstance(value, str):
                return Response(
                    {"error": f"{field.replace('_', ' ').title()} must be text."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            value = value.strip()
            if required and not value:
                return Response(
                    {"error": f"{field.replace('_', ' ').title()} is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if len(value) > max_length:
                return Response(
                    {"error": f"{field.replace('_', ' ').title()} is too long."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            cleaned[field] = value

        name = cleaned["name"]
        email = cleaned["email"].lower()
        phone = cleaned["phone"]
        company = cleaned["company"]
        lead_source = cleaned["lead_source"]
        referral_name = cleaned["referral_name"]
        referral_code = cleaned["referral_code"].upper()
        attribution_token = request.data.get("attribution_token")
        message = cleaned["message"]

        try:
            validate_email(email)
        except DjangoValidationError:
            return Response(
                {"error": "Enter a valid email address."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with schema_context(get_public_schema_name()):
            with transaction.atomic():
                from .models import Lead
                # Network timeouts can cause a browser to retry a request whose
                # first write already succeeded. Reuse only an identical, very
                # recent submission so a retry does not create duplicate leads.
                lead = Lead.objects.filter(
                    email__iexact=email,
                    name=name,
                    phone=phone,
                    company_name=company,
                    referral_name=referral_name,
                    message=message,
                    created_at__gte=timezone.now() - timedelta(minutes=2),
                ).first()
                lead_created = lead is None
                if lead_created:
                    lead = Lead.objects.create(
                        name=name,
                        email=email,
                        phone=phone,
                        company_name=company,
                        lead_source=lead_source,
                        referral_name=referral_name,
                        message=message,
                    )
                affiliate_referral = None
                try:
                    from apps.affiliate.services import record_affiliate_signup
                    affiliate_referral = record_affiliate_signup(
                        referral_code=referral_code,
                        attribution_token=attribution_token,
                        email=email,
                        company_name=company,
                        lead=lead,
                    )
                except Exception:
                    logger.exception("Affiliate attribution failed for lead %s", lead.id)
                if affiliate_referral and lead.lead_source != "Affiliate Referral":
                    lead.lead_source = "Affiliate Referral"
                    lead.save(update_fields=["lead_source"])
                elif (
                    referral_code
                    and not affiliate_referral
                    and lead.lead_source.lower() == "affiliate referral"
                ):
                    # Preserve the acquisition signal if the code could not be
                    # linked to an active affiliate account.
                    lead.lead_source = "Referral link (unverified)"
                    lead.save(update_fields=["lead_source"])

        logger.info(
            "Public lead captured lead_id=%s created=%s affiliate_attributed=%s request_id=%s",
            lead.id,
            lead_created,
            bool(affiliate_referral),
            request.headers.get("X-Request-ID", "-"),
        )

        # Persist first, then hand notification delivery to the worker. Telegram
        # latency or an outage must never hold the browser submission open.
        telegram_configured = bool(
            getattr(settings, "TELEGRAM_BOT_TOKEN", "")
            and getattr(settings, "TELEGRAM_ADMIN_CHAT_IDS", [])
        )
        if lead_created and telegram_configured:
            try:
                from apps.notifications.tasks import send_telegram_lead_alert_by_id
                send_telegram_lead_alert_by_id.apply_async(args=[lead.id], retry=False)
            except Exception:
                # Redis being unavailable should not lose the alert. Use a
                # direct best-effort fallback after the lead is already saved.
                logger.exception("Could not queue Telegram alert for lead %s", lead.id)
                try:
                    from apps.notifications.services.lead_alert_service import deliver_telegram_lead_alert
                    deliver_telegram_lead_alert(lead.id)
                except Exception:
                    logger.exception("Direct Telegram fallback failed for lead %s", lead.id)
        elif lead_created:
            logger.error(
                "Lead %s saved, but Telegram alerts are not configured. "
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_IDS.",
                lead.id,
            )

        import threading
        def _send_lead_email():
            try:
                from django.core.mail import send_mail
                send_mail(
                    subject=f"New Lead: {name} ({company or 'No company'})",
                    message=(
                        f"New lead submitted:\n\n"
                        f"Name: {name}\n"
                        f"Email: {email}\n"
                        f"Phone: {phone}\n"
                        f"Company: {company}\n"
                        f"Lead Source: {lead.lead_source or 'Not specified'}\n"
                        f"Referred By: {referral_name or 'Not specified'}\n"
                        f"Affiliate Referral: {'Yes' if affiliate_referral else 'No'}\n"
                        f"Affiliate Code: {affiliate_referral.affiliate.referral_code if affiliate_referral else 'Not applicable'}\n"
                        f"Message: {message}"
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[settings.DEFAULT_FROM_EMAIL],
                    fail_silently=True,
                )
            except Exception:
                pass
        if lead_created:
            threading.Thread(target=_send_lead_email, daemon=True).start()

        return Response({
            "message": "Thank you! We'll be in touch shortly.",
            "lead_id": lead.id,
            "affiliate_attributed": bool(affiliate_referral),
        }, status=status.HTTP_201_CREATED if lead_created else status.HTTP_200_OK)


def _serialize_lead(lead):
    from apps.affiliate.services import affiliate_lead_data

    derived_status = "converted" if lead.is_contacted else "not_yet"
    return {
        "id": lead.id,
        "name": lead.name,
        "email": lead.email,
        "phone": lead.phone,
        "company_name": lead.company_name,
        "lead_source": lead.lead_source,
        "referral_name": lead.referral_name,
        "affiliate_referral": affiliate_lead_data(lead),
        "message": lead.message,
        "status": derived_status,
        "is_contacted": lead.is_contacted,
        "contacted_at": lead.contacted_at.isoformat() if lead.contacted_at else None,
        "created_at": lead.created_at.isoformat(),
    }


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "converted"}


class TenantLeadListView(APIView):
    """Tenant-local lead list and manual lead capture for ISP admins."""
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/leads"

    def get(self, request):
        from django.db.models import Q
        from .models import Lead

        page = max(1, int(request.query_params.get("page", 1)))
        page_size = min(100, max(1, int(request.query_params.get("page_size", 20))))
        search = request.query_params.get("search", "").strip()
        status_filter = request.query_params.get("status", "").strip()

        qs = Lead.objects.all().order_by("-created_at")
        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(email__icontains=search) |
                Q(phone__icontains=search) |
                Q(company_name__icontains=search) |
                Q(lead_source__icontains=search) |
                Q(referral_name__icontains=search) |
                Q(message__icontains=search)
            )
        if status_filter == "converted":
            qs = qs.filter(is_contacted=True)
        elif status_filter in {"not_yet", "new"}:
            qs = qs.filter(is_contacted=False)

        total = qs.count()
        start = (page - 1) * page_size
        results = [_serialize_lead(lead) for lead in qs[start:start + page_size]]
        return Response({
            "count": total,
            "next": None if start + page_size >= total else f"?page={page + 1}",
            "previous": None if page <= 1 else f"?page={page - 1}",
            "results": results,
        })

    def post(self, request):
        from .models import Lead

        name = (request.data.get("name") or "").strip()
        email = (request.data.get("email") or "").strip()
        phone = (request.data.get("phone") or "").strip()
        company_name = (request.data.get("company_name") or "").strip()
        lead_source = (request.data.get("lead_source") or "").strip()
        referral_name = (request.data.get("referral_name") or "").strip()
        message = (request.data.get("message") or "").strip()
        status_value = (request.data.get("status") or "not_yet").strip()

        if not name:
            return Response({"name": ["Lead name is required."]}, status=status.HTTP_400_BAD_REQUEST)
        if not phone and not email:
            return Response({"contact": ["Provide at least a phone number or email."]}, status=status.HTTP_400_BAD_REQUEST)

        converted = status_value == "converted" or _truthy(request.data.get("is_contacted"))
        lead = Lead.objects.create(
            name=name,
            email=email,
            phone=phone,
            company_name=company_name,
            lead_source=lead_source,
            referral_name=referral_name,
            message=message,
            is_contacted=converted,
            contacted_at=timezone.now() if converted else None,
        )
        return Response(_serialize_lead(lead), status=status.HTTP_201_CREATED)


class TenantLeadStatsView(APIView):
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/leads"

    def get(self, request):
        from .models import Lead

        total = Lead.objects.count()
        converted = Lead.objects.filter(is_contacted=True).count()
        not_yet = max(0, total - converted)
        return Response({
            "total": total,
            "converted": converted,
            "not_yet": not_yet,
            "conversion_rate": round((converted / total) * 100) if total else 0,
            "recent": Lead.objects.filter(created_at__gte=timezone.now() - timedelta(days=7)).count(),
        })


class TenantLeadDetailView(APIView):
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/leads"

    def patch(self, request, pk):
        from .models import Lead

        lead = get_object_or_404(Lead, pk=pk)
        for field in ("name", "email", "phone", "company_name", "lead_source", "referral_name", "message"):
            if field in request.data:
                setattr(lead, field, (request.data.get(field) or "").strip())

        if "status" in request.data or "is_contacted" in request.data:
            status_value = (request.data.get("status") or "").strip()
            converted = status_value == "converted" or _truthy(request.data.get("is_contacted"))
            lead.is_contacted = converted
            lead.contacted_at = timezone.now() if converted else None

        lead.save()
        return Response(_serialize_lead(lead))

    def delete(self, request, pk):
        from .models import Lead

        lead = get_object_or_404(Lead, pk=pk)
        lead.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ============================================================
# NEW: Unified Dashboard View - Single endpoint replacing 8+ calls
# ============================================================
class UnifiedDashboardView(APIView):
    """
    Single endpoint returning all dashboard data.
    Replaces 8+ separate API calls with one.
    
    GET /api/v1/core/dashboard/unified/
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin"

    def get(self, request):
        from django.db.models import Sum, Count, Q
        from django.utils import timezone
        from datetime import timedelta
        import concurrent.futures

        # ============================================================
        # FIX: Capture tenant schema BEFORE spawning threads
        # Each worker thread gets its own fresh DB connection that does
        # NOT inherit django-tenants' schema/search_path. Without this,
        # every query in the thread runs against the public schema.
        # ============================================================
        from django.db import connection
        tenant_schema = connection.schema_name
        can_view_revenue = self._can_view_revenue(request)

        # ============================================================
        # FIX: Convert to local time before computing day/week/month boundaries
        # ============================================================
        now = timezone.now()
        local_now = timezone.localtime(now)  # convert UTC -> Africa/Nairobi
        today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        
        # ============================================================
        # FIX: Use Monday-anchored calendar week instead of rolling 7 days
        # This matches weekly_income chart and fixes "This Week" revenue
        # ============================================================
        days_to_monday = today_start.weekday()  # Monday=0 ... Sunday=6
        week_start = today_start - timedelta(days=days_to_monday)   # ✅ Monday 00:00 of current week
        
        month_start = today_start.replace(day=1)
        prev_month_start = (month_start - timedelta(days=1)).replace(day=1)

        def get_customer_stats():
            with schema_context(tenant_schema):
                try:
                    from apps.customers.models import Customer
                    return Customer.objects.aggregate(
                        total=Count('id'),
                        active=Count('id', filter=Q(status='ACTIVE')),
                    )
                except Exception:
                    return {'total': 0, 'active': 0}

        def get_revenue_stats():
            with schema_context(tenant_schema):
                try:
                    from apps.billing.models.payment_models import Payment
                    pay = Payment.objects.filter(status__iexact='completed').aggregate(
                        today=Sum('amount', filter=Q(payment_date__gte=today_start)),
                        yesterday=Sum('amount', filter=Q(payment_date__gte=yesterday_start, payment_date__lt=today_start)),
                        week=Sum('amount', filter=Q(payment_date__gte=week_start)),
                        month=Sum('amount', filter=Q(payment_date__gte=month_start)),
                        prev_month=Sum('amount', filter=Q(payment_date__gte=prev_month_start, payment_date__lt=month_start)),
                        today_tx=Count('id', filter=Q(payment_date__gte=today_start)),
                    )
                    return pay
                except Exception:
                    return {}

        def get_router_stats():
            with schema_context(tenant_schema):
                try:
                    from apps.network.models import Router
                    stats = Router.objects.filter(is_active=True).aggregate(
                        total=Count('id'),
                        online=Count('id', filter=Q(status='online')),
                        offline=Count('id', filter=Q(status='offline')),
                        warning=Count('id', filter=Q(status='warning')),
                        maintenance=Count('id', filter=Q(status='maintenance')),
                    )
                    from apps.radius.models import RadAcct
                    connected = RadAcct.objects.filter(acctstoptime__isnull=True).count()
                    stats['total_connected_users'] = connected
                    return stats
                except Exception:
                    return {'total': 0, 'online': 0, 'offline': 0, 'warning': 0, 'maintenance': 0, 'total_connected_users': 0}

        def get_ticket_stats():
            with schema_context(tenant_schema):
                try:
                    from apps.support.models import SupportTicket as Ticket
                    return Ticket.objects.aggregate(
                        total=Count('id'),
                        open=Count('id', filter=Q(status__iexact='open')),
                        in_progress=Count('id', filter=Q(status__iexact='in_progress')),
                        resolved=Count('id', filter=Q(status__iexact='resolved')),
                    )
                except Exception:
                    return {'total': 0, 'open': 0, 'in_progress': 0, 'resolved': 0}

        def get_expired_count():
            with schema_context(tenant_schema):
                try:
                    from apps.radius.models import CustomerRadiusCredentials
                    return CustomerRadiusCredentials.objects.filter(
                        expiration_date__isnull=False,
                        expiration_date__lte=local_now,
                    ).count()
                except Exception:
                    return 0

        def get_active_subscriptions():
            with schema_context(tenant_schema):
                try:
                    from apps.radius.models import CustomerRadiusCredentials
                    from apps.billing.models.hotspot_models import HotspotSession

                    pppoe_count = CustomerRadiusCredentials.objects.filter(
                        is_enabled=True,
                    ).filter(
                        Q(expiration_date__isnull=True) | Q(expiration_date__gt=local_now)
                    ).count()

                    hotspot_active = HotspotSession.objects.filter(
                        status='active',
                        expires_at__gt=local_now,
                    ).values('hotspot_client_id').distinct().count()

                    return {'pppoe': pppoe_count, 'hotspot': hotspot_active, 'total': pppoe_count + hotspot_active}
                except Exception:
                    return {'pppoe': 0, 'hotspot': 0, 'total': 0}

        def get_online_count():
            with schema_context(tenant_schema):
                try:
                    from apps.radius.models import RadAcct
                    return RadAcct.objects.filter(acctstoptime__isnull=True).count()
                except Exception:
                    return 0

        def get_recent_activity():
            with schema_context(tenant_schema):
                try:
                    from apps.core.models import AuditLog
                    return list(AuditLog.objects.filter(
                        tenant=getattr(request, 'tenant', None)
                    ).order_by('-timestamp')[:10].values(
                        'id', 'user__email', 'action', 'model_name', 'object_repr', 'timestamp'
                    ))
                except Exception:
                    return []

        def get_weekly_income():
            with schema_context(tenant_schema):
                try:
                    from apps.billing.models.payment_models import Payment
                    days_to_monday = local_now.weekday()
                    this_week_start = today_start - timedelta(days=days_to_monday)
                    last_week_start = this_week_start - timedelta(days=7)

                    def week_buckets(start_dt, end_dt):
                        payments = Payment.objects.filter(
                            status__iexact='completed',
                            payment_date__gte=start_dt,
                            payment_date__lt=end_dt,
                        )
                        weekday_map = {i: 0 for i in range(7)}
                        for p in payments:
                            # FIX: Convert UTC to local timezone before calling .weekday()
                            local_dt = timezone.localtime(p.payment_date)
                            weekday_map[local_dt.weekday()] += float(p.amount or 0)
                        labels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
                        return [{'day': labels[i], 'amount': round(weekday_map[i], 2)} for i in range(7)]

                    return {
                        'this_week': week_buckets(this_week_start, local_now),
                        'last_week': week_buckets(last_week_start, this_week_start),
                    }
                except Exception:
                    return {'this_week': [], 'last_week': []}

        def get_monthly_earnings():
            with schema_context(tenant_schema):
                try:
                    from apps.billing.models.payment_models import Payment
                    from datetime import datetime
                    current_year = local_now.year
                    current_month = local_now.month
                    labels = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

                    def year_buckets(year, max_month):
                        result = []
                        for month in range(1, max_month + 1):
                            ms = datetime(year, month, 1, tzinfo=timezone.get_current_timezone())
                            me = datetime(year, month + 1, 1, tzinfo=timezone.get_current_timezone()) if month < 12 else datetime(year + 1, 1, 1, tzinfo=timezone.get_current_timezone())
                            total = float(Payment.objects.filter(
                                status__iexact='completed', payment_date__gte=ms, payment_date__lt=me
                            ).aggregate(v=Sum('amount'))['v'] or 0)
                            result.append({'month': labels[month - 1], 'amount': round(total, 2)})
                        return result

                    return {
                        'this_year': year_buckets(current_year, current_month),
                        'last_year': year_buckets(current_year - 1, 12),
                    }
                except Exception:
                    return {'this_year': [], 'last_year': []}

        # Run all queries in parallel using threads
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                'customers': executor.submit(get_customer_stats),
                'revenue': executor.submit(get_revenue_stats) if can_view_revenue else None,
                'routers': executor.submit(get_router_stats),
                'tickets': executor.submit(get_ticket_stats),
                'expired': executor.submit(get_expired_count),
                'subscriptions': executor.submit(get_active_subscriptions),
                'online': executor.submit(get_online_count),
                'activity': executor.submit(get_recent_activity),
                'weekly_income': executor.submit(get_weekly_income) if can_view_revenue else None,
                'monthly_earnings': executor.submit(get_monthly_earnings) if can_view_revenue else None,
            }
            results = {
                k: f.result() if f is not None else {}
                for k, f in futures.items()
            }

        rev = results['revenue']
        today_rev = float(rev.get('today') or 0)
        yesterday_rev = float(rev.get('yesterday') or 0)
        week_rev = float(rev.get('week') or 0)
        month_rev = float(rev.get('month') or 0)
        prev_month_rev = float(rev.get('prev_month') or 0)

        def pct_change(cur, prev):
            if not prev:
                return 0.0
            return round(((cur - prev) / prev) * 100, 1)

        r = results['routers']
        subs = results['subscriptions']
        income = results['weekly_income']
        earnings = results['monthly_earnings']

        return Response({
            'total_customers': results['customers'].get('total', 0),
            'active_customers': results['customers'].get('active', 0),
            'expired_customers': results['expired'],
            'active_subscriptions': subs,
            'online_count': results['online'],
            'routers': {
                'total_routers': r.get('total', 0),
                'online_routers': r.get('online', 0),
                'offline_routers': r.get('offline', 0),
                'warning_routers': r.get('warning', 0),
                'maintenance_routers': r.get('maintenance', 0),
                'total_connected_users': r.get('total_connected_users', 0),
            },
            'revenue': {
                'today': today_rev,
                'today_change': pct_change(today_rev, yesterday_rev),
                'week': week_rev,
                'month': month_rev,
                'month_change': pct_change(month_rev, prev_month_rev),
                'transactions_today': int(rev.get('today_tx') or 0),
            },
            'tickets': {
                'total': results['tickets'].get('total', 0),
                'open': results['tickets'].get('open', 0),
                'in_progress': results['tickets'].get('in_progress', 0),
                'resolved': results['tickets'].get('resolved', 0),
                'avg_response_time': '—',
            },
            'recent_activity': results['activity'],
            'overview': {
                'today_revenue': today_rev,
                'today_change': pct_change(today_rev, yesterday_rev),
                'week_revenue': week_rev,
                'month_revenue': month_rev,
                'month_change': pct_change(month_rev, prev_month_rev),
                'total_transactions_today': int(rev.get('today_tx') or 0),
                'weekly_income': income.get('this_week', []),
                'last_week_income': income.get('last_week', []),
                'monthly_earnings': earnings.get('this_year', []),
                'last_year_earnings': earnings.get('last_year', []),
            },
        })

    def _can_view_revenue(self, request) -> bool:
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False
        if getattr(user, "is_superuser", False):
            return True

        permission = HasRoleAccessPolicy()
        role = permission._normalize(getattr(user, "role", None))
        access_level = permission._normalize(getattr(user, "access_level", None))
        if role in permission.admin_roles or access_level in permission.admin_roles:
            return True

        custom_allowed = getattr(user, "custom_allowed_paths", None)
        if custom_allowed is not None:
            allowed = custom_allowed
        else:
            try:
                policy = RoleAccessPolicy.objects.filter(role=role).first()
                allowed = policy.allowed_paths if policy is not None else DEFAULT_ROLE_ACCESS_POLICIES.get(role, [])
            except Exception:
                allowed = DEFAULT_ROLE_ACCESS_POLICIES.get(role, [])

        finance_paths = (
            "/admin/payments",
            "/admin/invoices",
            "/admin/receipts",
            "/admin/analytics",
            "/admin/settings/billing",
        )
        return any(
            permission._path_is_allowed(allowed or [], path, "view")
            for path in finance_paths
        )
