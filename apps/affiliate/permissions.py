from rest_framework.permissions import BasePermission


class IsActiveAffiliate(BasePermission):
    message = "An active affiliate account is required."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        account = getattr(user, "affiliate_account", None) if user and user.is_authenticated else None
        return bool(user and user.is_active and account and account.status == "active")
