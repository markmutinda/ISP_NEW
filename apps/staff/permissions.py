from rest_framework.permissions import BasePermission
from django.contrib.auth import get_user_model

User = get_user_model()


class HRPermissions(BasePermission):
    """
    Permission class for HR department staff
    """
    
    def has_permission(self, request, view):
        # Check if user is authenticated
        if not request.user.is_authenticated:
            return False
        
        # Superusers and staff with HR access can perform HR actions
        if request.user.is_superuser:
            return True
        
        # Check if user has employee profile with HR access
        if hasattr(request.user, 'employee_profile'):
            employee = request.user.employee_profile
            
            # HR department employees have HR permissions
            if employee.department and employee.department.name.lower() == 'human resources':
                return True
            
            # Department managers have HR permissions for their department
            if employee.department and employee.department.manager == employee:
                return True
        
        return False
    
    def has_object_permission(self, request, view, obj):
        # Superusers can access any object
        if request.user.is_superuser:
            return True
        
        # Check if user has employee profile
        if not hasattr(request.user, 'employee_profile'):
            return False
        
        employee = request.user.employee_profile
        
        # HR department employees can access any object
        if employee.department and employee.department.name.lower() == 'human resources':
            return True
        
        # Department managers can access objects in their department
        if employee.department and employee.department.manager == employee:
            # Check if object belongs to employee's department
            if hasattr(obj, 'employee'):
                return obj.employee.department == employee.department
            elif hasattr(obj, 'department'):
                return obj.department == employee.department
        
        # Employees can access their own objects
        if hasattr(obj, 'employee'):
            return obj.employee.user == request.user
        
        return False


class ManagerPermissions(BasePermission):
    """
    Permission class for department managers
    """
    
    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False
        
        if request.user.is_superuser:
            return True
        
        if hasattr(request.user, 'employee_profile'):
            employee = request.user.employee_profile
            return employee.department and employee.department.manager == employee
        
        return False
