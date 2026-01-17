"""
Middleware for core functionality: audit logging, tenant switching, company context
"""

import json
import re
from django.utils.deprecation import MiddlewareMixin
from django.db import connection
from django.conf import settings
from django.http import HttpResponseForbidden
from django.core.exceptions import PermissionDenied

from .models import AuditLog, Tenant


# ================================
# Public machine-to-server endpoints
# These MUST bypass tenancy, audit logs, company checks, and auth assumptions
# ================================
PUBLIC_ROUTER_PATHS = (
    '/api/v1/network/routers/auth/',
    '/api/v1/network/routers/heartbeat/',
    '/api/v1/network/routers/script/',           # if you have public script endpoints
    '/api/v1/network/routers/config/',           # add others if needed
)


class CompanyContextMiddleware(MiddlewareMixin):
    """
    Attaches request.company and request.tenant for authenticated users.
    Enforces company isolation and provides context for views.
    """

    def process_request(self, request):
        # 1. Skip completely for public router/machine endpoints
        if any(request.path.startswith(path) for path in PUBLIC_ROUTER_PATHS):
            return None

        # 2. Skip if no user or not authenticated
        if not hasattr(request, 'user') or not request.user.is_authenticated:
            return None

        user = request.user

        # 3. Superuser: allow optional company/tenant override via query param
        if user.is_superuser:
            company_id = request.GET.get('company_id')
            tenant_id = request.GET.get('tenant_id')

            if company_id:
                from .models import Company
                try:
                    request.company = Company.objects.get(id=company_id)
                except Company.DoesNotExist:
                    request.company = None
            else:
                request.company = None

            if tenant_id:
                try:
                    request.tenant = Tenant.objects.get(id=tenant_id)
                except Tenant.DoesNotExist:
                    request.tenant = None
            else:
                request.tenant = None

            return None

        # 4. Normal authenticated user: use their assigned company/tenant
        request.company = getattr(user, 'company', None)
        request.tenant = getattr(user, 'tenant', None)

        # 5. Optional: strict enforcement — raise 403 if no company on protected paths
        # Uncomment if you want hard enforcement
        # protected_paths = ['/api/v1/customers/', '/api/v1/billing/', '/api/v1/network/']
        # if any(request.path.startswith(p) for p in protected_paths):
        #     if not request.company:
        #         raise PermissionDenied("No company context available for this request")

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

            # Use request.company / request.tenant if available
            tenant = getattr(request, 'tenant', None)
            company = getattr(request, 'company', None)

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
                company=company,  # if your AuditLog model has company field
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


class TenantMiddleware(MiddlewareMixin):
    """
    Middleware for multi-tenancy support using subdomain.
    Skips public router endpoints.
    """

    def process_request(self, request):
        if any(request.path.startswith(path) for path in PUBLIC_ROUTER_PATHS):
            return None

        host = request.get_host().split(':')[0]  # remove port if present
        parts = host.split('.')

        # Skip if no subdomain or localhost/dev
        if len(parts) < 3 or 'localhost' in host or '127.0.0.1' in host:
            return None

        subdomain = parts[0].lower()

        # Skip common subdomains (www, api, admin, etc.)
        if subdomain in ('www', 'api', 'admin', 'app', 'staging', 'dev'):
            return None

        try:
            tenant = Tenant.objects.get(subdomain=subdomain, is_active=True)
            connection.set_tenant(tenant)  # if you're using django-tenants
            request.tenant = tenant
        except Tenant.DoesNotExist:
            # Fail silently or return 404 - depending on your preference
            pass
        except Exception as e:
            # Log but don't crash
            pass

        return None