"""Superadmin: view a tenant's admin + staff and reset their passwords."""
import logging

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from django_tenants.utils import schema_context
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import AuditLog, Tenant, User
from .permissions import IsSuperAdmin
from .views import PROTECTED_SCHEMAS, _ensure_public, _log_action
from rest_framework.permissions import IsAuthenticated

logger = logging.getLogger(__name__)

DASHBOARD_ROLES = ("admin", "staff", "technician", "accountant", "support")
SYSTEM_EMAILS = {"admin@netily.co.ke", "admin@netily.io", "billing@netily.io"}


def _get_tenant(pk):
    _ensure_public()
    return get_object_or_404(
        Tenant.objects.select_related("company").exclude(schema_name__in=PROTECTED_SCHEMAS), pk=pk
    )


def _is_tenant_user(u):
    """Exclude mirrored platform superadmins and system identities."""
    return (
        (u.email or "").strip().lower() not in SYSTEM_EMAILS
        and not AuditLog._is_platform_superadmin_actor(u)
    )


def _serialize(u, primary_id):
    return {
        "id": u.id,
        "email": u.email or "",
        "first_name": u.first_name,
        "last_name": u.last_name,
        "full_name": u.get_full_name() or u.email or f"User {u.id}",
        "phone_number": u.phone_number or "",
        "role": u.role,
        "is_active": u.is_active,
        "is_primary_admin": u.id == primary_id,
        "last_login": u.last_login.isoformat() if u.last_login else None,
        "date_joined": u.date_joined.isoformat() if u.date_joined else None,
    }


def _load_users(tenant):
    with schema_context(tenant.schema_name):
        rows = list(User.objects.filter(role__in=DASHBOARD_ROLES).order_by("date_joined", "id"))
    return [u for u in rows if _is_tenant_user(u)]


class TenantStaffView(APIView):
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request, pk):
        tenant = _get_tenant(pk)
        users = _load_users(tenant)
        admins = [u for u in users if u.role == "admin"]
        primary = admins[0] if admins else None          # created at tenant registration
        primary_id = primary.id if primary else None
        return Response({
            "tenant_id": str(tenant.id),
            "company_name": tenant.company.name if tenant.company else tenant.subdomain,
            "admin": _serialize(primary, primary_id) if primary else None,
            "other_admins": [_serialize(u, primary_id) for u in admins[1:]],
            "staff": [_serialize(u, primary_id) for u in users if u.role != "admin"],
        })


class TenantStaffPasswordResetView(APIView):
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def post(self, request, pk, user_id):
        tenant = _get_tenant(pk)
        new_password = str((request.data or {}).get("new_password") or "")
        if len(new_password) < 8:
            return Response({"detail": "Password must be at least 8 characters."},
                            status=status.HTTP_400_BAD_REQUEST)

        with schema_context(tenant.schema_name):
            user = User.objects.filter(pk=user_id, role__in=DASHBOARD_ROLES).first()
            if not user or not _is_tenant_user(user):
                return Response({"detail": "User not found in this tenant."},
                                status=status.HTTP_404_NOT_FOUND)
            try:
                validate_password(new_password, user=user)
            except DjangoValidationError as exc:
                return Response({"detail": " ".join(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)

            user.set_password(new_password)
            # password change rotates the session hash -> existing JWTs are rejected
            user.save(update_fields=["password", "updated_at"])
            target_email, target_role = user.email, user.role

        _ensure_public()
        _log_action(
            request.user, "update", "TenantUserPassword",
            object_repr=f"{tenant.subdomain}: {target_email}",
            object_id=user_id,
            changes={"event": "superadmin_password_reset", "role": target_role,
                     "tenant": tenant.schema_name},
            request=request,
        )
        return Response({"detail": f"Password updated for {target_email}."})