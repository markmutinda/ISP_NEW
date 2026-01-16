"""
Middleware for core functionality
"""

import json
import re
from django.utils.deprecation import MiddlewareMixin
from django.db import connection
from django.conf import settings

from .models import AuditLog

# ================================
# Public machine-to-server endpoints
# These MUST bypass tenancy, audit logs, and auth assumptions
# ================================
PUBLIC_ROUTER_PATHS = (
    '/api/v1/network/routers/auth/',
    '/api/v1/network/routers/heartbeat/',
)


class AuditLogMiddleware(MiddlewareMixin):
    """
    Middleware to log authenticated user actions.
    Skips machine endpoints (routers, heartbeats, etc.)
    """

    def process_request(self, request):
        # Skip audit logging for router endpoints
        if request.path.startswith(PUBLIC_ROUTER_PATHS):
            return None

        request.audit_log_info = {
            'ip_address': self.get_client_ip(request),
            'user_agent': request.META.get('HTTP_USER_AGENT', ''),
        }
        return None

    def process_response(self, request, response):
        # Skip audit logging for router endpoints
        if request.path.startswith(PUBLIC_ROUTER_PATHS):
            return response

        if (
            hasattr(request, 'audit_log_info')
            and hasattr(request, 'user')
            and request.user.is_authenticated
            and request.method in ['POST', 'PUT', 'PATCH', 'DELETE']
        ):
            self.log_action(request, response)

        return response

    def get_client_ip(self, request):
        """Get client IP address safely"""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR')

    def log_action(self, request, response):
        """Log the action to AuditLog"""
        try:
            path = request.path
            model_name = self.extract_model_name(path)
            object_id = self.extract_object_id(path)
            action = self.get_action_type(request.method)

            changes = None
            if request.method in ['POST', 'PUT', 'PATCH']:
                try:
                    body = request.body.decode('utf-8')
                    if body:
                        changes = json.loads(body)
                except Exception:
                    changes = {'data': 'Unable to parse'}

            AuditLog.objects.create(
                user=request.user,
                action=action,
                model_name=model_name,
                object_id=object_id,
                object_repr=str(object_id) if object_id else '',
                changes=changes,
                ip_address=request.audit_log_info.get('ip_address'),
                user_agent=request.audit_log_info.get('user_agent'),
            )
        except Exception:
            # Never break requests due to logging failure
            pass

    def extract_model_name(self, path):
        """Extract model name from URL path"""
        match = re.search(r'/api/v\d+/(\w+)/', path)
        return match.group(1) if match else 'unknown'

    def extract_object_id(self, path):
        """Extract object ID from URL path"""
        match = re.search(r'/api/v\d+/\w+/(\d+)/', path)
        return match.group(1) if match else None

    def get_action_type(self, method):
        """Map HTTP method to action type"""
        return {
            'POST': 'create',
            'PUT': 'update',
            'PATCH': 'update',
            'DELETE': 'delete',
        }.get(method, 'view')


class TenantMiddleware(MiddlewareMixin):
    """
    Middleware for multi-tenancy support.
    Applies ONLY to user-facing endpoints.
    """

    def process_request(self, request):
        # Skip tenant resolution for router endpoints
        if request.path.startswith(PUBLIC_ROUTER_PATHS):
            return None

        host = request.get_host()
        subdomain = self.extract_subdomain(host)

        if not subdomain:
            return None

        try:
            from .models import Tenant
            tenant = Tenant.objects.get(subdomain=subdomain, is_active=True)
            connection.set_tenant(tenant)
            request.tenant = tenant
        except Exception:
            # Fail silently to avoid breaking public or unknown domains
            pass

        return None

    def extract_subdomain(self, host):
        """
        Extract subdomain safely.
        example:
        customer.yourisp.com -> customer
        """
        host = host.split(':')[0]  # rem
