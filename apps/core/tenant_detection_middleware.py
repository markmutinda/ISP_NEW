"""
apps/core/tenant_detection_middleware.py
"""
from django.utils.deprecation import MiddlewareMixin
from django_tenants.utils import get_tenant_model
from django.db import connection

Tenant = get_tenant_model()


class CustomTenantDetectionMiddleware(MiddlewareMixin):
    """
    Helps django-tenants detect subdomain.localhost patterns
    """
    def process_request(self, request):
        host = request.get_host().split(':')[0]
        
        # Check for *.localhost pattern
        if host.endswith('.localhost') and host != 'localhost':
            subdomain = host.split('.')[0]
            
            # Set the subdomain in request so django-tenants can use it
            request.tenant_subdomain = subdomain
            
            # Also try to set HTTP_HOST to a format django-tenants understands
            # This helps django-tenants' default domain detection
            if hasattr(request, 'META'):
                # Add a fake domain that django-tenants will recognize
                request.META['HTTP_X_FORWARDED_HOST'] = f"{subdomain}.example.com"
        
        return None