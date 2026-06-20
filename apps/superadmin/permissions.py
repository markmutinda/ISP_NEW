"""
Superadmin Permissions
─────────────────────
Only is_superuser=True users can access any superadmin view.
These permissions run in the PUBLIC schema context.
"""

from rest_framework.permissions import BasePermission


class IsSuperAdmin(BasePermission):
    """
    Allow access only to users with `is_superuser=True`.
    This is the single gate for every superadmin endpoint.
    """

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.is_superuser
        )


class IsPlatformSupport(BasePermission):
    """Allow superadmins and active platform support executives."""

    message = "Platform support access is required."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated or not user.is_active:
            return False
        if user.is_superuser:
            return True

        profile = getattr(user, "platform_support_profile", None)
        return bool(profile and profile.is_active)
