from rest_framework import permissions
from django.contrib.auth import get_user_model

User = get_user_model()

class IsAdminOrStaff(permissions.BasePermission):
    """Allow access only to admin or staff users"""
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated and (
            request.user.is_superuser or request.user.is_staff
        )

class CanManageNotifications(permissions.BasePermission):
    """Permission for managing notifications"""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Admins can do everything
        if request.user.is_superuser:
            return True
        
        # Staff with specific permissions
        if request.user.is_staff:
            # Check for specific permissions based on view action
            if view.action in ['list', 'retrieve']:
                return True
            elif view.action in ['create', 'update', 'partial_update', 'destroy']:
                # Only allow if user has specific permission
                return request.user.has_perm('notifications.manage_notifications')
        
        return False

class CanSendNotifications(permissions.BasePermission):
    """Permission for sending notifications"""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Admins and staff can send
        if request.user.is_superuser or request.user.is_staff:
            return True
        
        # Check for specific permission
        return request.user.has_perm('notifications.send_notifications')

class CanManageTemplates(permissions.BasePermission):
    """Permission for managing notification templates"""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Only admins and staff with specific permission
        return (
            request.user.is_superuser or 
            request.user.has_perm('notifications.manage_templates')
        )

class CanManageAlertRules(permissions.BasePermission):
    """Permission for managing alert rules"""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Only admins and network/technical staff
        return (
            request.user.is_superuser or 
            request.user.has_perm('notifications.manage_alerts') or
            request.user.groups.filter(name__in=['Network Admins', 'Technical Staff']).exists()
        )

class CanSendBulkNotifications(permissions.BasePermission):
    """Permission for sending bulk notifications"""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Only admins and marketing staff
        return (
            request.user.is_superuser or 
            request.user.has_perm('notifications.send_bulk') or
            request.user.groups.filter(name='Marketing').exists()
        )

class CanViewOwnNotifications(permissions.BasePermission):
    """Users can view their own notifications"""
    def has_object_permission(self, request, view, obj):
        # Users can view their own notifications
        if hasattr(obj, 'user') and obj.user == request.user:
            return True
        
        # Users can view notifications sent to their email or phone
        if hasattr(obj, 'recipient_email') and obj.recipient_email == request.user.email:
            return True
        
        if hasattr(obj, 'recipient_phone') and obj.recipient_phone == request.user.phone:
            return True
        
        return False

class CanManageOwnPreferences(permissions.BasePermission):
    """Users can manage their own notification preferences"""
    def has_object_permission(self, request, view, obj):
        return obj.user == request.user

class IsCustomerSelfService(permissions.BasePermission):
    """Allow customers to access their own notifications in self-service"""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        
        # Customers can access their own notifications
        if request.user.role == 'customer':
            return True
        
        return False
    
    def has_object_permission(self, request, view, obj):
        # Customers can only access their own notifications
        if hasattr(obj, 'user'):
            return obj.user == request.user
        return False