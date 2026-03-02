"""
Middleware for core functionality: audit logging, tenant switching, company context
"""

import json
import re
from django.utils.deprecation import MiddlewareMixin
from django.db import connection
from django.conf import settings
from django.http import HttpResponseForbidden, HttpResponse
from django.core.exceptions import PermissionDenied

from .models import AuditLog, Tenant, Domain, Company


# ================================
# Public machine-to-server endpoints
# These MUST bypass tenancy, audit logs, company checks, and auth assumptions
# ================================
PUBLIC_ROUTER_PATHS = (
    '/api/v1/network/routers/auth/',
    '/api/v1/network/routers/heartbeat/',
    '/api/v1/network/routers/script/',
    '/api/v1/network/routers/config/',
    '/api/v1/hotspot/captive-portal/',
    '/api/v1/hotspot/login-page/',
    '/api/v1/hotspot/purchase/',
)


class CorsPreflightMiddleware(MiddlewareMixin):
    """
    Handle CORS preflight requests before any other processing.
    This ensures OPTIONS requests always get CORS headers even if other middleware fails.
    """
    
    def process_request(self, request):
        if request.method == 'OPTIONS':
            response = HttpResponse()
            response['Access-Control-Allow-Origin'] = request.META.get('HTTP_ORIGIN', '*')
            response['Access-Control-Allow-Methods'] = 'GET, POST, PUT, PATCH, DELETE, OPTIONS'
            response['Access-Control-Allow-Headers'] = 'Accept, Accept-Language, Content-Type, Authorization, X-CSRFToken, X-Requested-With, X-Tenant'
            response['Access-Control-Allow-Credentials'] = 'true'
            response['Access-Control-Max-Age'] = '86400'
            return response
        return None
    
    def process_response(self, request, response):
        # Add CORS headers to all responses
        origin = request.META.get('HTTP_ORIGIN', '*')
        if 'Access-Control-Allow-Origin' not in response:
            response['Access-Control-Allow-Origin'] = origin
        if 'Access-Control-Allow-Credentials' not in response:
            response['Access-Control-Allow-Credentials'] = 'true'
        return response


class TenantMainMiddleware(MiddlewareMixin):
    """
    Custom tenant middleware that handles both local and production subdomains.
    - Local:      bluenet.localhost:8000
    - Production: bluenet.netily.co.ke
    - API host:   api.netily.co.ke  → public schema
    This replaces django_tenants.middleware.main.TenantMainMiddleware
    """

    # Known base domains — add more as needed
    BASE_DOMAINS = ['localhost', 'netily.co.ke', 'netily.io', 'netily.com']
    # Subdomains that should NOT be treated as tenants
    RESERVED_SUBDOMAINS = {'www', 'api', 'admin', 'app', 'mail', 'smtp', 'ftp', 'cdn', 'static'}

    def _extract_subdomain(self, host):
        """Return (subdomain, base_domain) or (None, host) for main/API domain."""
        for base in self.BASE_DOMAINS:
            if host == base:
                return None, base
            if host.endswith(f'.{base}'):
                sub = host[: -(len(base) + 1)]
                if sub and sub not in self.RESERVED_SUBDOMAINS:
                    return sub, base
                return None, base
        return None, host

    def _resolve_tenant(self, subdomain, full_host):
        """Try to find a tenant by subdomain, then by Domain record.
        Returns (tenant, company) or (None, None).
        """
        connection.set_schema_to_public()
        try:
            tenant = Tenant.objects.get(subdomain=subdomain, is_active=True)
        except Tenant.DoesNotExist:
            # Fallback: look up by exact domain record
            try:
                domain = Domain.objects.get(domain=full_host)
                tenant = domain.tenant
            except Domain.DoesNotExist:
                return None, None

        company = None
        try:
            company = tenant.company
        except Exception:
            pass
        return tenant, company

    def process_request(self, request):
        # Skip public machine-to-server endpoints
        if any(request.path.startswith(p) for p in PUBLIC_ROUTER_PATHS):
            connection.set_schema_to_public()
            request.tenant = None
            request.company = None
            return None

        host = request.get_host().split(':')[0]  # Remove port
        subdomain, _ = self._extract_subdomain(host)

        if subdomain:
            tenant, company = self._resolve_tenant(subdomain, host)
            if tenant:
                connection.set_tenant(tenant)
                request.tenant = tenant
                request.company = company
                return None
            # Unknown subdomain → fall through to public

        # Main domain / API domain / unknown → public schema
        connection.set_schema_to_public()
        request.tenant = None
        request.company = None
        return None


class CompanyContextMiddleware(MiddlewareMixin):
    """
    Attaches request.company and request.tenant for authenticated users
    """
    def process_request(self, request):
        # If tenant is already set by TenantMainMiddleware, use it
        if hasattr(request, 'tenant') and request.tenant:
            # Company is already set by TenantMainMiddleware
            return None
        
        # For authenticated users, get their company/tenant
        if hasattr(request, 'user') and request.user.is_authenticated:
            request.company = getattr(request.user, 'company', None)
            request.tenant = getattr(request.user, 'tenant', None)
        
        return None


class AuditLogMiddleware(MiddlewareMixin):
    """
    Middleware to log authenticated user actions.
    Skips machine endpoints (routers, heartbeats, etc.)
    """

    def process_request(self, request):
        if any(request.path.startswith(path) for path in PUBLIC_ROUTER_PATHS):
            return None

        request.audit_log_info = {
            'ip_address': self.get_client_ip(request),
            'user_agent': request.META.get('HTTP_USER_AGENT', ''),
        }
        return None

    def process_response(self, request, response):
        if any(request.path.startswith(path) for path in PUBLIC_ROUTER_PATHS):
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
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR')

    def log_action(self, request, response):
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

            tenant = getattr(request, 'tenant', None)

            AuditLog.objects.create(
                user=request.user,
                action=action,
                model_name=model_name,
                object_id=object_id,
                object_repr=str(object_id) if object_id else '',
                changes=changes,
                ip_address=request.audit_log_info.get('ip_address'),
                user_agent=request.audit_log_info.get('user_agent'),
                tenant=tenant,
            )
        except Exception:
            # Never break the request due to logging failure
            pass

    def extract_model_name(self, path):
        match = re.search(r'/api/v\d+/(\w+)/', path)
        return match.group(1) if match else 'unknown'

    def extract_object_id(self, path):
        match = re.search(r'/api/v\d+/\w+/([^/]+)/', path)
        return match.group(1) if match else None

    def get_action_type(self, method):
        return {
            'POST': 'create',
            'PUT': 'update',
            'PATCH': 'update',
            'DELETE': 'delete',
        }.get(method, 'view')