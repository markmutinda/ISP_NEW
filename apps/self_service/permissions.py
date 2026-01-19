from rest_framework import permissions


class CustomerOnlyPermission(permissions.BasePermission):
    """
    Permission that only allows customers to access their own data
    """
    
    def has_permission(self, request, view):
        # Check if user is authenticated
        if not request.user.is_authenticated:
            return False
        
        # Check if user has customer profile
        if not hasattr(request.user, 'customer_profile'):
            return False
        
        return True
    
    def has_object_permission(self, request, view, obj):
        # Allow staff/admin to access all objects
        if request.user.is_staff or request.user.is_superuser:
            return True
        
        # Customers can only access their own data
        if hasattr(obj, 'customer'):
            return obj.customer == request.user.customer_profile
        
        # For customer-specific objects
        if hasattr(obj, 'user'):
            return obj.user == request.user
        
        return False
