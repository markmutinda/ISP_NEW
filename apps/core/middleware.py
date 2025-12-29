"""
Middleware for core functionality
"""
import json
from django.utils.deprecation import MiddlewareMixin
from .models import AuditLog
from django.db import connection
import re


class AuditLogMiddleware(MiddlewareMixin):
    """Middleware to log user actions"""
    
    def process_request(self, request):
        """Store request information for logging"""
        request.audit_log_info = {
            'ip_address': self.get_client_ip(request),
            'user_agent': request.META.get('HTTP_USER_AGENT', ''),
        }
        return None
    
    def process_response(self, request, response):
        """Log actions after response is generated"""
        if hasattr(request, 'audit_log_info') and request.user.is_authenticated:
            # Only log certain actions
            if request.method in ['POST', 'PUT', 'PATCH', 'DELETE']:
                self.log_action(request, response)
        return response
    
    def get_client_ip(self, request):
        """Get client IP address"""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip
    
    def log_action(self, request, response):
        """Log the action to AuditLog"""
        try:
            # Extract model name from URL
            path = request.path
            model_name = self.extract_model_name(path)
            
            # Extract object ID from URL
            object_id = self.extract_object_id(path)
            
            # Determine action type
            action = self.get_action_type(request.method)
            
            # Get changes for POST/PUT/PATCH
            changes = None
            if request.method in ['POST', 'PUT', 'PATCH']:
                try:
                    # Try to get data from request body
                    body = request.body.decode('utf-8')
                    if body:
                        changes = json.loads(body)
                except:
                    changes = {'data': 'Unable to parse'}
            
            # Create audit log entry
            AuditLog.objects.create(
                user=request.user,
                action=action,
                model_name=model_name,
                object_id=object_id,
                object_repr=str(object_id),
                changes=changes,
                ip_address=request.audit_log_info['ip_address'],
                user_agent=request.audit_log_info['user_agent']
            )
        except Exception as e:
            # Don't crash the request if logging fails
            pass
    
    def extract_model_name(self, path):
        """Extract model name from URL path"""
        # Match patterns like /api/customers/ or /api/users/1/
        match = re.search(r'/api/(\w+)/', path)
        if match:
            return match.group(1)
        return 'unknown'
    
    def extract_object_id(self, path):
        """Extract object ID from URL path"""
        # Match patterns like /api/model/123/
        match = re.search(r'/api/\w+/(\d+)/', path)
        if match:
            return match.group(1)
        return None
    
    def get_action_type(self, method):
        """Map HTTP method to action type"""
        action_map = {
            'POST': 'create',
            'PUT': 'update',
            'PATCH': 'update',
            'DELETE': 'delete',
        }
        return action_map.get(method, 'view')


class TenantMiddleware(MiddlewareMixin):
    """Middleware for multi-tenancy support"""
    
    def process_request(self, request):
        """Set tenant context based on subdomain"""
        host = request.get_host()
        
        # Extract subdomain from host
        subdomain = self.extract_subdomain(host)
        
        if subdomain:
            # Set database connection to tenant's database
            from .models import Tenant
            try:
                tenant = Tenant.objects.get(subdomain=subdomain, is_active=True)
                connection.set_tenant(tenant)
                request.tenant = tenant
            except Tenant.DoesNotExist:
                # Use default database for unknown tenants
                pass
        
        return None
    
    def extract_subdomain(self, host):
        """Extract subdomain from host"""
        parts = host.split('.')
        if len(parts) > 2:
            return parts[0]
        return None