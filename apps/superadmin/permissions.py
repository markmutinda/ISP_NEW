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
