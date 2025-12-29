"""
Custom permission classes for ISP Management System
"""
from rest_framework import permissions
from django.contrib.auth import get_user_model

User = get_user_model()


class IsAdmin(permissions.BasePermission):
    """
    Allows access only to admin users.
    """
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and 
                   (request.user.role == 'admin' or request.user.is_superuser))


class IsAdminOrStaff(permissions.BasePermission):
    """
    Allows access to admin or staff users.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        allowed_roles = ['admin', 'staff', 'accountant', 'support']
        return request.user.role in allowed_roles or request.user.is_superuser


class IsTechnician(permissions.BasePermission):
    """
    Allows access only to technician users.
    """
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and 
                   request.user.role == 'technician')


class IsCustomer(permissions.BasePermission):
    """
    Allows access only to customer users.
    """
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and 
                   request.user.role == 'customer')


class IsAdminOrTechnician(permissions.BasePermission):
    """
    Allows access to admin or technician users.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        return (request.user.role in ['admin', 'technician'] or 
                request.user.is_superuser)


class IsStaffOrTechnician(permissions.BasePermission):
    """
    Allows access to staff or technician users.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        return request.user.role in ['staff', 'technician', 'admin'] or request.user.is_superuser


class IsOwnerOrAdmin(permissions.BasePermission):
    """
    Allows access to object owner or admin users.
    """
    def has_object_permission(self, request, view, obj):
        # Admin can do anything
        if request.user.role == 'admin' or request.user.is_superuser:
            return True
        
        # Check if user owns the object
        if hasattr(obj, 'user'):
            return obj.user == request.user
        elif hasattr(obj, 'customer'):
            return obj.customer.user == request.user
        elif hasattr(obj, 'created_by'):
            return obj.created_by == request.user
        
        return False


class IsCompanyMember(permissions.BasePermission):
    """
    Allows access only to users in the same company.
    """
    def has_object_permission(self, request, view, obj):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Admin can access all
        if request.user.role == 'admin' or request.user.is_superuser:
            return True
        
        # Check if object has company attribute
        if hasattr(obj, 'company'):
            return obj.company == request.user.company
        elif hasattr(obj, 'customer') and hasattr(obj.customer, 'company'):
            return obj.customer.company == request.user.company
        
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
    Allows access to users who can manage other users.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Admin can manage all users
        if request.user.role == 'admin' or request.user.is_superuser:
            return True
        
        # Staff can manage customers and technicians
        if request.user.role == 'staff' and view.action in ['list', 'retrieve', 'create', 'update']:
            return True
        
        return False


class CanViewDashboard(permissions.BasePermission):
    """
    Allows access to dashboard based on user role.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Everyone except customers can view the main dashboard
        if view.action == 'dashboard' and request.user.role != 'customer':
            return True
        
        # Customers can view their own dashboard
        if view.action == 'customer_dashboard' and request.user.role == 'customer':
            return True
        
        return True