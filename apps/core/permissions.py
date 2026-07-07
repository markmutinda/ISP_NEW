"""
Custom permission classes for ISP Management System - Updated for Multi-Tenancy
"""
from rest_framework import permissions


class HasCompanyAccess(permissions.BasePermission):
    """
    Permission class that ensures users can only access data from their own company.
    Superusers can access all data.
    
    Usage: Use this in ViewSets where get_queryset() does company filtering
    """
    
    def has_permission(self, request, view):
        # Allow all authenticated users to access the view
        # Actual company filtering happens in get_queryset
        return request.user and request.user.is_authenticated
    
    def has_object_permission(self, request, view, obj):
        # Superusers can do anything
        if request.user.is_superuser:
            return True
        
        # User must have a company
        if not hasattr(request.user, 'company') or not request.user.company:
            return False
        
        # Check object's company ownership using various patterns
        # 1a. Direct company ForeignKey field
        if hasattr(obj, 'company') and hasattr(obj.company, 'pk'):
            return obj.company == request.user.company
        
        # 1b. Denormalised company_name CharField (e.g. Router model)
        if hasattr(obj, 'company_name') and isinstance(getattr(type(obj), 'company_name', None), property) is False:
            user_company_name = getattr(request.user.company, 'name', None)
            if user_company_name and obj.company_name == user_company_name:
                return True
        
        # 1c. Tenant-subdomain ownership (multi-tenant Router pattern)
        if hasattr(obj, 'tenant_subdomain') and hasattr(request, 'tenant'):
            if obj.tenant_subdomain and obj.tenant_subdomain == getattr(request.tenant, 'schema_name', None):
                return True
        
        # 2. Through customer
        if hasattr(obj, 'customer') and hasattr(obj.customer, 'company'):
            return obj.customer.company == request.user.company
        
        # 3. Through subnet
        if hasattr(obj, 'subnet') and hasattr(obj.subnet, 'company'):
            return obj.subnet.company == request.user.company
        
        # 4. Through OLT
        if hasattr(obj, 'olt') and hasattr(obj.olt, 'company'):
            return obj.olt.company == request.user.company
        
        # 5. Through router
        if hasattr(obj, 'router') and hasattr(obj.router, 'company'):
            return obj.router.company == request.user.company
        
        # 6. Through CPE device
        if hasattr(obj, 'cpe_device') and hasattr(obj.cpe_device, 'company'):
            return obj.cpe_device.company == request.user.company
        
        # 7. Through invoice
        if hasattr(obj, 'invoice') and hasattr(obj.invoice, 'company'):
            return obj.invoice.company == request.user.company
        
        # 8. Through voucher batch
        if hasattr(obj, 'batch') and hasattr(obj.batch, 'company'):
            return obj.batch.company == request.user.company
        
        # 9. For User objects - check if user belongs to same company
        if hasattr(obj, 'company'):
            return obj.company == request.user.company
        
        # If we can't determine company relationship, be conservative and deny
        return False


class IsCompanyAdmin(permissions.BasePermission):
    """
    Allows access only to company admin users.
    Superusers are also considered company admins.
    """
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Superusers are admins
        if request.user.is_superuser:
            return True
        
        if request.user.role != 'admin':
            return False

        # Public-schema users have a real FK; tenant-schema users often carry
        # tenant/company context through middleware plus denormalized fields.
        if getattr(request.user, 'company', None):
            return True

        if getattr(request, 'company', None) or getattr(request, 'tenant', None):
            return True

        if getattr(request.user, 'company_name', None) or getattr(request.user, 'tenant_subdomain', None):
            return True
        
        return False


class IsCompanyStaff(permissions.BasePermission):
    """
    Allows access to company staff users (admin, staff, technician, accountant, support).
    Superusers are also allowed.
    """
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Superusers are staff
        if request.user.is_superuser:
            return True
        
        allowed_roles = ['admin', 'staff', 'technician', 'accountant', 'support']
        if request.user.role not in allowed_roles:
            return False

        if getattr(request.user, 'company', None):
            return True

        if getattr(request, 'company', None) or getattr(request, 'tenant', None):
            return True

        if getattr(request.user, 'company_name', None) or getattr(request.user, 'tenant_subdomain', None):
            return True
        
        return False


class IsCompanyMember(permissions.BasePermission):
    """
    Allows access to any user belonging to a company (including customers).
    Superusers are also allowed.
    """
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Superusers are members
        if request.user.is_superuser:
            return True
        
        # Any authenticated user with a company is a member
        if hasattr(request.user, 'company') and request.user.company:
            return True
        
        return False


class IsCompanyTechnician(permissions.BasePermission):
    """
    Allows access only to technician users within a company.
    Superusers are also allowed.
    """
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Superusers are technicians
        if request.user.is_superuser:
            return True
        
        # User must have a company and be a technician role
        if hasattr(request.user, 'company') and request.user.company:
            return request.user.role == 'technician'
        
        return False


class IsCompanyCustomer(permissions.BasePermission):
    """
    Allows access only to customer users within a company.
    Superusers are also allowed.
    """
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Superusers are customers (for access purposes)
        if request.user.is_superuser:
            return True
        
        # User must have a company and be a customer role
        if hasattr(request.user, 'company') and request.user.company:
            return request.user.role == 'customer'
        
        return False


class IsOwnerOrCompanyAdmin(permissions.BasePermission):
    """
    Allows access to object owner or company admin users.
    """
    
    def has_object_permission(self, request, view, obj):
        # Superusers and company admins can do anything
        if request.user.is_superuser:
            return True
        
        if hasattr(request.user, 'company') and request.user.company:
            if request.user.role == 'admin':
                return True
        
        # Check if user owns the object
        if hasattr(obj, 'user'):
            return obj.user == request.user
        elif hasattr(obj, 'customer'):
            return obj.customer.user == request.user
        elif hasattr(obj, 'created_by'):
            return obj.created_by == request.user
        
        return False


class ReadOnly(permissions.BasePermission):
    """
    Allows read-only access for all users.
    """
    
    def has_permission(self, request, view):
        return request.method in permissions.SAFE_METHODS


class IsAuthenticatedAndVerified(permissions.BasePermission):
    """
    Allows access only to authenticated and verified users.
    """
    
    def has_permission(self, request, view):
        return bool(
            request.user and 
            request.user.is_authenticated and 
            request.user.is_verified
        )


class CanManageUsers(permissions.BasePermission):
    """
    Allows access to users who can manage other users within the same company.
    """
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Superusers can manage all users
        if request.user.is_superuser:
            return True
        
        # Check if user has a company
        if not hasattr(request.user, 'company') or not request.user.company:
            return False
        
        # Company admins can manage all users in their company
        if request.user.role == 'admin':
            return True
        
        # Company staff can manage customers and technicians (but not other staff/admins)
        if request.user.role == 'staff':
            allowed_actions = ['list', 'retrieve', 'create', 'update']
            if view.action in allowed_actions:
                # Check if trying to manage a user in same company
                if 'pk' in view.kwargs:
                    from django.shortcuts import get_object_or_404
                    from .models import User
                    target_user = get_object_or_404(User, pk=view.kwargs['pk'])
                    # Staff can only manage users in same company and with role 'customer' or 'technician'
                    return (hasattr(target_user, 'company') and 
                            target_user.company == request.user.company and
                            target_user.role in ['customer', 'technician'])
                return True
        
        return False


class CanViewDashboard(permissions.BasePermission):
    """
    Allows access to dashboard based on user role and company.
    """
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Everyone can view some form of dashboard
        return True


# ============================================================================
# ALIASES for backward compatibility and billing app
# ============================================================================

class IsAdmin(IsCompanyAdmin):
    """
    Alias for IsCompanyAdmin for backward compatibility.
    """
    pass


class IsAdminOrStaff(IsCompanyStaff):
    """
    Alias for IsCompanyStaff for backward compatibility.
    """
    pass


class HasRoleAccessPolicy(permissions.BasePermission):
    """
    Enforce tenant RoleAccessPolicy records for admin API endpoints.

    Views opt in by setting `required_rbac_path`, for example
    `required_rbac_path = "/admin/payments"`. Plain path grants remain
    backward compatible; action tokens use `/admin/path::action`.
    """

    action_map = {
        "list": "view",
        "dashboard_stats": "view",
        "retrieve": "view_details",
        "create": "add",
        "update": "edit",
        "partial_update": "edit",
        "destroy": "delete",
        "mark_completed": "edit",
        "mark_failed": "edit",
        "reconcile": "edit",
        "refund": "edit",
        "issue": "edit",
        "share": "view_details",
    }

    admin_roles = {"admin", "super_admin", "superadmin"}

    def _normalize(self, value):
        return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")

    def _required_action(self, request, view):
        action = self.action_map.get(getattr(view, "action", None))
        if action:
            return action
        if request.method in permissions.SAFE_METHODS:
            return "view"
        if request.method == "POST":
            return "add"
        if request.method in {"PUT", "PATCH"}:
            return "edit"
        if request.method == "DELETE":
            return "delete"
        return "view"

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return False
        if getattr(user, "is_superuser", False):
            return True

        role = self._normalize(getattr(user, "role", None))
        access_level = self._normalize(getattr(user, "access_level", None))
        if role in self.admin_roles or access_level in self.admin_roles:
            return True

        required_path = getattr(view, "required_rbac_path", None)
        if not required_path:
            return True

        try:
            from apps.core.models import RoleAccessPolicy

            policy = RoleAccessPolicy.objects.filter(role=role).first()
        except Exception:
            return True

        if not policy:
            return True

        allowed = policy.allowed_paths or []
        if not allowed:
            return False

        required_action = self._required_action(request, view)
        action_token = f"{required_path}::{required_action}"
        if action_token in allowed:
            return True

        has_action_tokens = any(path.startswith(f"{required_path}::") for path in allowed)
        if has_action_tokens:
            return False

        return any(required_path == path or required_path.startswith(f"{path}/") for path in allowed)


class IsTechnician(IsCompanyTechnician):
    """
    Alias for IsCompanyTechnician for backward compatibility.
    """
    pass


class IsCustomer(IsCompanyCustomer):
    """
    Alias for IsCompanyCustomer for backward compatibility.
    """
    pass


class IsAdminOrTechnician(permissions.BasePermission):
    """
    Allows access to admin or technician users within a company.
    """
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if request.user.is_superuser:
            return True
        
        if hasattr(request.user, 'company') and request.user.company:
            return request.user.role in ['admin', 'technician']
        
        return False


class IsStaffOrTechnician(permissions.BasePermission):
    """
    Allows access to staff or technician users within a company.
    """
    
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        if request.user.is_superuser:
            return True
        
        if hasattr(request.user, 'company') and request.user.company:
            return request.user.role in ['admin', 'staff', 'technician', 'accountant', 'support']
        
        return False


class IsOwnerOrAdmin(IsOwnerOrCompanyAdmin):
    """
    Alias for IsOwnerOrCompanyAdmin for backward compatibility.
    """
    pass


# ============================================================================
# COMPOSITE PERMISSIONS for specific use cases
# ============================================================================

class CompanyStaffOrReadOnly(permissions.BasePermission):
    """
    Allows read-only access to all, but write access only to company staff.
    """
    
    def has_permission(self, request, view):
        # Read permissions are allowed to any request
        if request.method in permissions.SAFE_METHODS:
            return True
        
        # Write permissions are only allowed to company staff
        staff_permission = IsCompanyStaff()
        return staff_permission.has_permission(request, view)


class CompanyAdminOrReadOnly(permissions.BasePermission):
    """
    Allows read-only access to all, but write access only to company admins.
    """
    
    def has_permission(self, request, view):
        # Read permissions are allowed to any request
        if request.method in permissions.SAFE_METHODS:
            return True
        
        # Write permissions are only allowed to company admins
        admin_permission = IsCompanyAdmin()
        return admin_permission.has_permission(request, view)


class CustomerOrCompanyStaff(permissions.BasePermission):
    """
    Allows access to customers for their own data, and company staff for all.
    """
    
    def has_permission(self, request, view):
        # Company staff can do anything
        staff_permission = IsCompanyStaff()
        if staff_permission.has_permission(request, view):
            return True
        
        # Customers can access certain views
        customer_permission = IsCompanyCustomer()
        return customer_permission.has_permission(request, view)


# ============================================================================
# PERMISSION UTILITIES
# ============================================================================

def check_company_object_permission(user, obj):
    """
    Utility function to check if a user has permission to access an object
    based on company membership.
    
    Usage in views or serializers where you need to check permissions.
    """
    if user.is_superuser:
        return True
    
    if not hasattr(user, 'company') or not user.company:
        return False
    
    # Check various company ownership patterns
    if hasattr(obj, 'company'):
        return obj.company == user.company
    
    if hasattr(obj, 'customer') and hasattr(obj.customer, 'company'):
        return obj.customer.company == user.company
    
    if hasattr(obj, 'subnet') and hasattr(obj.subnet, 'company'):
        return obj.subnet.company == user.company
    
    if hasattr(obj, 'olt') and hasattr(obj.olt, 'company'):
        return obj.olt.company == user.company
    
    if hasattr(obj, 'router') and hasattr(obj.router, 'company'):
        return obj.router.company == user.company
    
    if hasattr(obj, 'invoice') and hasattr(obj.invoice, 'company'):
        return obj.invoice.company == user.company
    
    return False
