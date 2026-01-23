""""
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
    Custom tenant middleware that properly handles subdomain.localhost
    This replaces django_tenants.middleware.main.TenantMainMiddleware
    """
    
    def process_request(self, request):
        # Get the host from the request
        host = request.get_host().split(':')[0]  # Remove port
        
        # Skip if it's a public router endpoint or API endpoint
        if request.path.startswith('/api/v1/network/routers/'):
            return None
        
        # Check for subdomain.localhost pattern
        if host.endswith('.localhost') and host != 'localhost':
            # Extract subdomain (e.g., "dansted" from "dansted.localhost")
            subdomain = host.split('.')[0]
            
            try:
                # First, ensure we're in public schema to find tenant
                connection.set_schema_to_public()
                
                # Find the tenant by subdomain in public schema
                tenant = Tenant.objects.get(subdomain=subdomain, is_active=True)
                
                # Get company from tenant (in public schema)
                company = None
                try:
                    company = tenant.company
                except:
                    # Company might not be accessible or doesn't exist
                    pass
                
                # Now switch to tenant schema for the rest of the request
                connection.set_tenant(tenant)
                
                # Set request attributes
                request.tenant = tenant
                request.company = company  # This is the company object from public schema
                
                print(f"DEBUG: Switched to tenant: {tenant.subdomain}, company: {company.name if company else 'None'}")  # Debug
                
            except Tenant.DoesNotExist:
                # Tenant not found - check if we have a domain record
                try:
                    # Check Domain model for the exact domain
                    domain = Domain.objects.get(domain=host)
                    tenant = domain.tenant
                    
                    # Get company
                    company = None
                    try:
                        company = tenant.company
                    except:
                        pass
                    
                    connection.set_tenant(tenant)
                    request.tenant = tenant
                    request.company = company
                    
                    print(f"DEBUG: Switched to tenant via Domain: {tenant.subdomain}")  # Debug
                    
                except Domain.DoesNotExist:
                    # No tenant found - use public schema
                    connection.set_schema_to_public()
                    request.tenant = None
                    request.company = None
                    print(f"DEBUG: No tenant found for host: {host}, using public schema")  # Debug
        
        else:
            # For localhost or other hosts, use public schema
            connection.set_schema_to_public()
            request.tenant = None
            request.company = None
        
        return None


class CompanyContextMiddleware(MiddlewareMixin):
    """
    Attaches request.company and request.tenant for authenticated users
    """
    def process_request(self, request):
        # Skip for public router endpoints
        if request.path.startswith('/api/v1/network/routers/'):
            return None
        
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