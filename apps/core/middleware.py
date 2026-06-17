"""
Middleware for core functionality: audit logging, tenant switching, company context
"""

import json
import re
import logging
from django.utils.deprecation import MiddlewareMixin
from django.db import connection
from django.conf import settings
from django.http import HttpResponseForbidden, HttpResponse, JsonResponse
from django.core.exceptions import PermissionDenied

from .models import AuditLog, Tenant, Domain, Company

# Get logger for this module
logger = logging.getLogger(__name__)


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
    '/api/v1/hotspot/routers/',
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
    - Custom domains: bentrextechnologies.com → tenant lookup via Domain model
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

        host = request.get_host().split(':')[0].lower()  # Force lowercase for absolute matching matchers
        subdomain, _ = self._extract_subdomain(host)

        # 1. First, attempt standard platform subdomain matching pipelines
        if subdomain:
            tenant, company = self._resolve_tenant(subdomain, host)
        else:
            # 2. 🚀 FIX: If no platform subdomain is found, look up the domain record index directly
            connection.set_schema_to_public()
            try:
                domain = Domain.objects.select_related('tenant').get(domain=host)
                tenant = domain.tenant
                
                # Check active lifecycle states
                if tenant and not tenant.is_active:
                    tenant = None
                    
                company = None
                if tenant:
                    try:
                        company = tenant.company
                    except Exception:
                        pass
            except Domain.DoesNotExist:
                tenant, company = None, None

        # 3. If a valid custom domain or subdomain tenant matches, switch context schemas
        if tenant:
            connection.set_tenant(tenant)
            request.tenant = tenant
            request.company = company
            return None

        # Main domain / API domain / unknown → fallback safely to public schema container
        connection.set_schema_to_public()
        request.tenant = None
        request.company = None
        return None


class CompanyContextMiddleware(MiddlewareMixin):
    """
    Attaches request.company and request.tenant for authenticated users.
    For superadmin users on a tenant subdomain, temporarily sets
    request.user.company so tenant views that filter by user.company work.
    """
    def process_request(self, request):
        # If tenant is already set by TenantMainMiddleware, use it
        if hasattr(request, 'tenant') and request.tenant:
            # Company is already set by TenantMainMiddleware.
            # Patch authenticated tenant users with the resolved company context
            # when their FK fields are intentionally left null inside tenant schemas.
            if hasattr(request, 'user') and request.user.is_authenticated:
                tenant_company = getattr(request.tenant, 'company', None)
                if tenant_company:
                    if not getattr(request.user, 'company', None):
                        request.user._original_company = None
                        request.user.company = tenant_company
                    if not getattr(request.user, 'tenant', None):
                        request.user._original_tenant = None
                        request.user.tenant = request.tenant
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


# ─────────────────────────────────────────────────────────────
# FIX 3: SUBSCRIPTION ENFORCEMENT MIDDLEWARE (P1)
# ─────────────────────────────────────────────────────────────
# This middleware enforces a hard lockout when an ISP's subscription
# is past_due or trial has expired. It blocks all API requests
# except those needed to settle the invoice (billing, subscriptions, auth).
# This ensures ISPs cannot continue using the platform without paying.
# ─────────────────────────────────────────────────────────────

class SubscriptionEnforcementMiddleware(MiddlewareMixin):
    """
    Enforces a hard lockout if the tenant's subscription is past_due or expired.
    Allows access to billing/payment endpoints so they can settle the invoice.
    
    Returns HTTP 402 Payment Required for blocked requests.
    """
    
    def process_request(self, request):
        # 1. Skip public machine endpoints (routers must keep tracking usage)
        if any(request.path.startswith(p) for p in PUBLIC_ROUTER_PATHS):
            return None
        
        # 2. Only block API endpoints
        if not request.path.startswith('/api/'):
            return None
        
        # 3. Restrict allowlist to EXACTLY what is needed to pay the bill
        #    This ensures ISPs cannot access ANY platform functionality until payment is made.
        #    Only the minimal endpoints for viewing lockout status, making payment,
        #    and checking payment status are allowed.
        ALLOWED_PATHS = [
            '/api/v1/subscriptions/current/',  # View their locked status
            '/api/v1/subscriptions/pay/',      # Initiate M-Pesa payment
            '/api/v1/subscriptions/payments/', # Poll payment status
            '/api/v1/subscriptions/plans/',    # View available plans (to select & pay)
            '/api/v1/core/auth/',              # Login/Logout/OTP/Token refresh
            '/api/v1/core/users/me/',          # Identity check (needed after login for auth context)
        ]
        
        # Check if the request path starts with any allowed path
        if any(request.path.startswith(p) for p in ALLOWED_PATHS):
            return None
        
        # 4. Superadmin users bypass subscription enforcement entirely
        if hasattr(request, 'user') and hasattr(request.user, 'is_superuser') and request.user.is_superuser:
            return None

        # 5. Hard block manually suspended tenants for all non-payment API work.
        tenant = getattr(request, 'tenant', None)
        if tenant and getattr(tenant, 'status', None) == 'suspended':
            company_name = getattr(getattr(request, 'company', None), 'name', None) or getattr(tenant, 'subdomain', 'tenant')
            logger.warning("Blocked suspended tenant API request for %s: %s", company_name, request.path)
            return JsonResponse({
                'error': 'tenant_suspended',
                'message': 'This ISP workspace is currently suspended. Please contact Netily Support to restore access.',
                'code': 'TENANT_SUSPENDED',
                'status': 'suspended',
            }, status=403)

        # 6. Check subscription state
        
        # Only enforce if we have a tenant and company
        if tenant and hasattr(request, 'company') and request.company:
            # Use public schema to access subscription models
            from django_tenants.utils import schema_context
            
            with schema_context('public'):
                from apps.subscriptions.models import CompanySubscription, BillingCycle
                
                try:
                    sub = CompanySubscription.objects.select_related('plan').get(company=request.company)
                    
                    # Check if subscription is locked (past_due or overdue billing only)
                    # NOTE: trial_expired is NOT enforced here — the frontend TrialGuard
                    # dialog handles expired trials/periods with an uncloseable payment wall,
                    # allowing dashboard data to load behind it for a better UX.
                    is_locked = False
                    lock_reason = None
                    
                    if sub.status == 'past_due':
                        is_locked = True
                        lock_reason = 'Your subscription payment is past due. Please settle your invoice to restore access.'
                    elif sub.status == 'active':
                        # ── Fallback: catch overdue billing cycles the beat task missed ──
                        from django.utils import timezone as tz
                        overdue_cycle = BillingCycle.objects.filter(
                            tenant=tenant,
                            subscription=sub,
                            status='invoiced',
                            grace_ends_at__lt=tz.now(),
                        ).exists()
                        if overdue_cycle:
                            sub.status = 'past_due'
                            sub.save(update_fields=['status'])
                            is_locked = True
                            lock_reason = 'Your subscription payment is past due. Please settle your invoice to restore access.'
                    
                    if is_locked:
                        logger.warning(
                            f"Blocked API request for {request.company.name}: {request.path} - "
                            f"Reason: {lock_reason}"
                        )
                        
                        return JsonResponse({
                            'error': 'payment_required',
                            'message': lock_reason,
                            'code': 'SUBSCRIPTION_EXPIRED',
                            'status': sub.status,
                            'trial_expired': sub.trial_expired,
                            'allowed_endpoints': [
                                '/api/v1/subscriptions/current/',
                                '/api/v1/subscriptions/pay/',
                                '/api/v1/subscriptions/payments/',
                                '/api/v1/subscriptions/plans/',
                                '/api/v1/auth/',
                            ]
                        }, status=402)  # HTTP 402 Payment Required
                        
                except CompanySubscription.DoesNotExist:
                    # No subscription found - this is a new company still in setup
                    # Allow access but log for monitoring
                    logger.debug(f"No subscription found for {request.company.name}")
                    pass
                except Exception as e:
                    # Log error but don't block - better to let through than lock out due to error
                    logger.error(f"Error checking subscription status for {request.company.name}: {e}")
        
        return None