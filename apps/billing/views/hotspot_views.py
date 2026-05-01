"""
Hotspot Views for Captive Portal Payments

These are PUBLIC endpoints - no authentication required.
End users access these when connecting to WiFi hotspots.
"""

import logging
import random
import string
import time
import hashlib
import base64
from decimal import Decimal

from django.conf import settings
from django.db import transaction, ProgrammingError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.core.cache import cache  # ADDED: For TV code support

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from django_tenants.utils import schema_context, get_public_schema_name

from apps.billing.models.hotspot_models import HotspotPlan, HotspotSession, HotspotBranding
from apps.billing.models.billing_models import Plan
from apps.billing.models.payment_models import Payment, TenantTumaConfig, InvoiceItemPayment
from apps.billing.models.voucher_models import Voucher
from apps.billing.services.tuma_service import TumaClient
from apps.billing.integrations.mpesa_integration import MpesaSTKPush
from apps.core.models import TumaCallbackMap
from apps.network.models.router_models import Router
from apps.subscriptions.models import CommissionLedger

logger = logging.getLogger(__name__)


# ── At the top of the file, add this constant after the imports ──
MPESA_CAPABLE_METHOD_TYPES = [
    'MPESA_STK', 'MPESA_PAYBILL', 'MPESA_TILL', 'MOBILE_MONEY', 'MPESA',
]


# ============================================================
# HELPER UTILITIES
# ============================================================

def _normalize_mac(mac: str) -> str:
    """Normalize MAC address to uppercase with colons."""
    return (mac or "").upper().replace("-", ":").strip()


def _canonical_phone(phone: str) -> str:
    """Canonicalize phone number to 254 format."""
    digits = ''.join(ch for ch in (phone or '') if ch.isdigit())
    if not digits:
        return ''
    if digits.startswith('0'):
        return '254' + digits[1:]
    if digits.startswith('254'):
        return digits
    return '254' + digits


def _plan_data_limit_display(plan):
    """Human-readable data limit string for a Plan model object."""
    if plan.data_limit is None:
        return 'Unlimited'
    if plan.data_limit >= 1024:
        tb = plan.data_limit / 1024
        return f'{tb:g} TB'
    return f'{plan.data_limit} GB'


def _plan_validity_value(plan):
    """Extract the raw validity value from a Plan model object."""
    vtype = (plan.validity_type or 'DAYS').upper()
    if vtype == 'MINUTES':
        return plan.validity_minutes or 0
    elif vtype == 'HOURS':
        return plan.validity_hours or 0
    elif vtype == 'MONTHS':
        return plan.validity_months or 0
    elif vtype == 'UNLIMITED':
        return 0
    return plan.duration_days or 30


def _serialize_plan(plan):
    """Serialize a Plan (billing_models.Plan) to the captive-portal response format."""
    return {
        'id': str(plan.id),
        'name': plan.name,
        'description': plan.description or '',
        'price': float(plan.base_price),
        'currency': 'KES',
        'validity_type': plan.validity_type or 'DAYS',
        'validity_value': _plan_validity_value(plan),
        'duration_display': plan.validity_display,
        'download_speed': plan.download_speed or 0,
        'upload_speed': plan.upload_speed or 0,
        'speed_unit': plan.speed_unit or 'MBPS',
        'speed_display': plan.speed_display,
        'limitation_type': 'UNLIMITED' if plan.data_limit is None else 'DATA',
        'data_limit_value': plan.data_limit,
        'data_limit_unit': 'GB',
        'data_limit_display': _plan_data_limit_display(plan),
        'is_popular': plan.is_popular,
    }


def _serialize_hotspot_plan(plan):
    """Serialize a HotspotPlan to the captive-portal response format."""
    return {
        'id': str(plan.id),
        'name': plan.name,
        'description': plan.description or '',
        'price': float(plan.price),
        'currency': plan.currency,
        'validity_type': plan.validity_type,
        'validity_value': plan.validity_value,
        'duration_display': plan.duration_display,
        'download_speed': plan.download_speed,
        'upload_speed': plan.upload_speed,
        'speed_unit': plan.speed_unit,
        'speed_display': plan.speed_display,
        'limitation_type': plan.limitation_type,
        'data_limit_value': plan.data_limit_value,
        'data_limit_unit': plan.data_limit_unit,
        'data_limit_display': plan.data_limit_display,
        'simultaneous_devices': plan.simultaneous_devices,
        'is_popular': plan.is_popular,
    }


def _close_prior_radacct_rows_for_renewal(session, username: str):
    """
    Close open radacct rows that belong to a prior subscription period.

    We anchor on session.activated_at (the CURRENT session's start time).
    Any open row that started BEFORE this session was activated belongs to
    a previous period and must be closed so usage resets correctly.
    """
    from apps.radius.models import RadAcct

    if not username:
        return 0

    # Only meaningful for returning clients (hotspot_client links sessions)
    if not session.hotspot_client:
        return 0

    # Need a concrete start time to anchor the cut-off
    if not session.activated_at:
        return 0

    closed = RadAcct.objects.filter(
        username=username,
        acctstoptime__isnull=True,
        acctstarttime__lt=session.activated_at,   # ← was prev.activated_at (wrong)
    ).update(
        acctstoptime=timezone.now(),
        acctterminatecause='Session-Timeout',
    )

    if closed:
        logger.info(
            "Closed %d prior-period radacct row(s) for %s "
            "(new period started %s)",
            closed, username, session.activated_at.isoformat(),
        )

    return closed


class CaptivePortalView(APIView):
    """
    Public captive-portal endpoint — returns portal config + plans for a
    given router, resolving the tenant explicitly via query parameters.

    GET /api/v1/hotspot/captive-portal/?router={router_id}&tenant={tenant}

    Plan resolution order:
      1. HotspotPlan records linked to this specific router (primary)
      2. Fallback: Plan records with plan_type='HOTSPOT' (tenant-wide)

    This is the canonical endpoint for the public WiFi captive portal pages.
    It does NOT require authentication.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    # FIX 1: Optimized with caching and leaner DB field selection
    def get(self, request):
        router_id = request.query_params.get('router')
        tenant_subdomain = request.query_params.get('tenant')

        if not router_id or not tenant_subdomain:
            return Response(
                {
                    'message': 'Both "router" and "tenant" query parameters are required.',
                    'received_router': router_id,
                    'received_tenant': tenant_subdomain,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ---- FAST PATH: short-lived cache (safe, no behavior change) ----
        cache_key = f"hotspot:captive:v1:{tenant_subdomain}:{router_id}"
        cached_payload = cache.get(cache_key)
        if cached_payload is not None:
            return Response(cached_payload)

        try:
            from apps.core.models import Tenant
            with schema_context(get_public_schema_name()):
                tenant = Tenant.objects.get(
                    Q(subdomain=tenant_subdomain) | Q(schema_name=tenant_subdomain),
                    is_active=True
                )
        except Exception as e:
            logger.error(f"Tenant '{tenant_subdomain}' not found: {e}")
            return Response({'status': 'error', 'message': 'Tenant not found'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with schema_context(tenant.schema_name):
                # Router lookup - safe fallback without field restrictions
                router_qs = Router.objects.filter(is_active=True)

                router = None
                try:
                    router = router_qs.get(id=router_id)
                except (Router.DoesNotExist, ValueError):
                    try:
                        router = router_qs.get(name=router_id)
                        logger.info("CaptivePortal: router found by name '%s' -> id=%s", router_id, router.id)
                    except Router.DoesNotExist:
                        logger.warning(f"Router '{router_id}' does not exist in tenant {tenant_subdomain}")
                except ProgrammingError:
                    logger.warning("CaptivePortal: network_router table missing for tenant %s", tenant_subdomain)

                if router is None:
                    portal_config = {
                        'template_id': 1,
                        'hotspot_name': tenant_subdomain,
                        'support_phone': '',
                        'announcement_text': '',
                        'gateway_ip': '',
                    }
                    branding_data = None
                else:
                    portal_config = {
                        'template_id': router.template_id or 1,
                        'hotspot_name': router.hotspot_name or router.name,
                        'support_phone': router.support_phone or '',
                        'announcement_text': router.announcement_text or '',
                        'gateway_ip': router.gateway_ip,
                    }

                    branding_data = None
                    try:
                        branding = getattr(router, 'hotspot_branding', None)
                        if branding is None:
                            branding = HotspotBranding.objects.filter(is_default=True).only(
                                'company_name', 'logo', 'background_image',
                                'primary_color', 'secondary_color', 'text_color', 'background_color',
                                'welcome_title', 'welcome_message', 'support_phone', 'support_email'
                            ).first()
                        if branding:
                            branding_data = {
                                'company_name': branding.company_name,
                                'logo_url': branding.logo.url if branding.logo else None,
                                'background_image_url': branding.background_image.url if branding.background_image else None,
                                'primary_color': branding.primary_color,
                                'secondary_color': branding.secondary_color,
                                'text_color': branding.text_color,
                                'background_color': branding.background_color,
                                'welcome_title': branding.welcome_title,
                                'welcome_message': branding.welcome_message,
                                'support_phone': branding.support_phone,
                                'support_email': branding.support_email,
                            }
                            if not portal_config['support_phone'] and branding.support_phone:
                                portal_config['support_phone'] = branding.support_phone
                    except Exception:
                        logger.debug("CaptivePortal: no branding found for router %s", router_id)

                plans_data = []
                if router is not None:
                    try:
                        hotspot_plans = HotspotPlan.objects.filter(
                            router=router,
                            is_active=True,
                        ).only(
                            'id', 'name', 'description', 'price', 'currency',
                            'validity_type', 'validity_value',
                            'download_speed', 'upload_speed', 'speed_unit',
                            'limitation_type', 'data_limit_value', 'data_limit_unit',
                            'simultaneous_devices', 'is_popular', 'duration_minutes', 'data_limit_mb'
                        ).order_by('sort_order', 'price')
                        plans_data = [_serialize_hotspot_plan(p) for p in hotspot_plans]
                    except ProgrammingError:
                        logger.warning("CaptivePortal: billing_hotspotplan table missing for tenant %s", tenant_subdomain)

                if not plans_data:
                    try:
                        generic_plans = Plan.objects.filter(
                            plan_type='HOTSPOT',
                            is_active=True,
                        ).only(
                            'id', 'name', 'description', 'base_price',
                            'validity_type', 'validity_minutes', 'validity_hours', 'validity_months', 'duration_days',
                            'download_speed', 'upload_speed', 'speed_unit',
                            'data_limit', 'is_popular'
                        ).order_by('is_popular', 'base_price')
                        plans_data = [_serialize_plan(p) for p in generic_plans]
                        if plans_data:
                            logger.info(
                                "CaptivePortal: using %d Plan(plan_type=HOTSPOT) fallback for tenant %s",
                                len(plans_data), tenant_subdomain,
                            )
                    except ProgrammingError:
                        logger.warning("CaptivePortal: billing_plan table missing for tenant %s", tenant_subdomain)

        except Exception as exc:
            logger.error(f"CaptivePortal internal error for tenant {tenant_subdomain}: {exc}")
            return Response({'status': 'error', 'message': 'Internal server error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        payload = {
            'status': 'success',
            'portal_config': portal_config,
            'branding': branding_data,
            'plans': plans_data,
        }

        # Keep cache short so admin updates appear quickly
        cache.set(cache_key, payload, timeout=30)
        return Response(payload)


class HotspotPlansView(APIView):
    """
    Get hotspot plans for a specific router.
    
    PUBLIC ENDPOINT - No authentication required.
    
    GET /api/v1/hotspot/routers/{router_id}/plans/
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []
    
    # FIX 2: Cached version with leaner DB field selection
    def get(self, request, router_id):
        cache_key = f"hotspot:plans:v1:{router_id}"
        cached_payload = cache.get(cache_key)
        if cached_payload is not None:
            return Response(cached_payload)

        router_qs = Router.objects.filter(is_active=True).only(
            'id', 'name', 'location', 'template_id', 'hotspot_name',
            'support_phone', 'announcement_text'
        )

        try:
            router = router_qs.get(id=router_id)
        except (Router.DoesNotExist, ValueError):
            try:
                router = router_qs.get(name=router_id)
                logger.info(f"Router found by name in HotspotPlansView: {router_id} -> {router.id}")
            except Router.DoesNotExist:
                return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)

        plans = HotspotPlan.objects.filter(
            router=router,
            is_active=True
        ).only(
            'id', 'name', 'price', 'currency', 'duration_minutes', 'data_limit_mb',
            'download_speed', 'upload_speed', 'speed_unit', 'description',
            'is_popular', 'validity_type', 'validity_value',
            'limitation_type', 'data_limit_value', 'data_limit_unit'
        ).order_by('sort_order', 'price')

        try:
            branding = router.hotspot_branding
        except HotspotBranding.DoesNotExist:
            branding = HotspotBranding.objects.filter(is_default=True).only(
                'company_name', 'logo', 'background_image',
                'primary_color', 'secondary_color', 'text_color', 'background_color',
                'welcome_title', 'welcome_message', 'support_phone', 'support_email'
            ).first()

        plans_data = [
            {
                'id': str(plan.id),
                'name': plan.name,
                'price': float(plan.price),
                'currency': plan.currency,
                'duration_minutes': plan.duration_minutes,
                'duration_display': plan.duration_display,
                'data_limit_mb': plan.data_limit_mb,
                'data_limit_display': plan.data_limit_display,
                'speed_limit': f"{plan.speed_limit_mbps}Mbps",
                'description': plan.description,
                'is_popular': plan.is_popular,
            }
            for plan in plans
        ]

        branding_data = None
        if branding:
            branding_data = {
                'company_name': branding.company_name,
                'logo_url': branding.logo.url if branding.logo else None,
                'background_image_url': branding.background_image.url if branding.background_image else None,
                'primary_color': branding.primary_color,
                'secondary_color': branding.secondary_color,
                'text_color': branding.text_color,
                'background_color': branding.background_color,
                'welcome_title': branding.welcome_title,
                'welcome_message': branding.welcome_message,
                'support_phone': branding.support_phone,
                'support_email': branding.support_email,
            }

        portal_config = {
            'template_id': router.template_id or 1,
            'hotspot_name': router.hotspot_name or router.name,
            'support_phone': router.support_phone or (branding.support_phone if branding else ''),
            'announcement_text': router.announcement_text or '',
        }

        payload = {
            'router': {
                'id': router.id,
                'name': router.name,
                'location': router.location,
            },
            'plans': plans_data,
            'branding': branding_data,
            'portal_config': portal_config,
        }

        cache.set(cache_key, payload, timeout=30)
        return Response(payload)


class HotspotPurchaseView(APIView):
    """
    Initiate hotspot purchase with REAL STK Push via Tuma.
    Creates pending session, initiates STK payment, and returns pending status.
    
    Supports TV pairing: if tv_code is provided, resolves MAC address server-side.
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []
    
    def generate_unique_code(self):
        """
        Generates a 9-character code like 'MXTV-827S'
        4 random chars + hyphen + 4 random chars
        """
        chars = string.ascii_uppercase + string.digits
        
        while True:
            part1 = ''.join(random.choices(chars, k=4))
            part2 = ''.join(random.choices(chars, k=4))
            code = f"{part1}-{part2}"
            
            if not HotspotSession.objects.filter(access_code=code).exists():
                return code

    def _get_active_hotspot_payment_method(self, schema_name: str):
        """
        Resolve tenant's active payment method for hotspot checkout.

        Accepts ANY active M-Pesa-capable method type.
        Priority: default first, then most recently updated active method.
        Falls back to ANY active method if no specific M-Pesa type found,
        since Tuma-configured methods may have various types.
        """
        # First try: explicit M-Pesa capable types
        method = (
            InvoiceItemPayment.objects
            .filter(
                schema_name=schema_name,
                method_type__in=MPESA_CAPABLE_METHOD_TYPES,
                is_active=True,
            )
            .select_related('mpesa_configuration', 'tuma_configuration')
            .order_by('-is_default', '-updated_at')
            .first()
        )
        
        if method:
            return method
        
        # Fallback: any active method that has a tuma_configuration OR mpesa_configuration
        # REMOVED: is_payhero_enabled filter
        method = (
            InvoiceItemPayment.objects
            .filter(
                schema_name=schema_name,
                is_active=True,
            )
            .filter(
                Q(tuma_configuration__isnull=False) |
                Q(mpesa_configuration__isnull=False)
            )
            .select_related('mpesa_configuration', 'tuma_configuration')
            .order_by('-is_default', '-updated_at')
            .first()
        )
        
        return method

    def _ensure_mpesa_stk_callback_url(self, mpesa_cfg):
        """
        Ensure STK callback URL points to the STK callback endpoint, not C2B.
        """
        if mpesa_cfg.callback_url:
            return

        base_url = getattr(settings, 'BASE_URL', '').rstrip('/')
        if base_url:
            mpesa_cfg.callback_url = f"{base_url}/api/v1/billing/mpesa/callback/"
            mpesa_cfg.save(update_fields=['callback_url', 'updated_at'])

    @transaction.atomic
    def post(self, request):
        tenant_subdomain = request.data.get('tenant') or request.query_params.get('tenant')
        if not tenant_subdomain:
            return Response({'error': 'Tenant is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from apps.core.models import Tenant
            with schema_context(get_public_schema_name()):
                tenant = Tenant.objects.get(subdomain=tenant_subdomain, is_active=True)
        except Exception:
            return Response({'error': 'Invalid tenant'}, status=status.HTTP_400_BAD_REQUEST)

        with schema_context(tenant.schema_name):
            router_id = request.data.get('router_id')
            plan_id = request.data.get('plan_id')
            phone_number = request.data.get('phone_number')
            mac_address = request.data.get('mac_address', '')
            tv_code = (request.data.get('tv_code') or '').strip().upper()
            
            if not all([router_id, plan_id, phone_number]):
                return Response({
                    'error': 'Missing required fields: router_id, plan_id, phone_number'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # ============================================================
            # TV CODE RESOLUTION: If tv_code provided, resolve MAC server-side
            # ============================================================
            reserved_access_code = None
            
            if tv_code:
                tv_cache_key = f"tv_code:{tenant.schema_name}:{tv_code}"
                tv_payload = cache.get(tv_cache_key)
                
                if not tv_payload:
                    return Response(
                        {'error': 'Invalid or expired TV code. Please refresh the TV screen and try again.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                
                # Bind purchase to the TV identity from cache
                mac_address = tv_payload.get('mac_address', '')
                router_id_from_code = tv_payload.get('router_id')
                
                if router_id_from_code:
                    router_id = router_id_from_code
                
                # Get the reserved access_code from the TV payload
                reserved_access_code = tv_payload.get('access_code') or tv_code
                
                logger.info(f"TV code {tv_code} resolved to MAC {mac_address}, router {router_id}, reserved access_code: {reserved_access_code}")
            
            try:
                router = Router.objects.get(id=router_id, is_active=True)
            except (Router.DoesNotExist, ValueError):
                try:
                    router = Router.objects.get(name=router_id, is_active=True)
                except Router.DoesNotExist:
                    return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)
            
            try:
                plan = HotspotPlan.objects.get(id=plan_id, router=router, is_active=True)
            except HotspotPlan.DoesNotExist:
                return Response({'error': 'Plan not found'}, status=status.HTTP_404_NOT_FOUND)
            
            mac_address = _normalize_mac(mac_address)
            
            # ══════════════════════════════════════════════════════════════
            # RESOLVE HOTSPOT IDENTITY
            # Identity priority: phone number → MAC → anonymous
            # The client's canonical_username IS their permanent RADIUS login.
            # ══════════════════════════════════════════════════════════════
            from apps.billing.models.hotspot_models import HotspotClient, HotspotClientDevice

            # Resolve the persistent client record
            hotspot_client = HotspotClient.get_or_create_by_mac(
                schema_name=tenant.schema_name,
                mac_address=mac_address,
                phone_number=phone_number,
            )

            # Register this specific device under the client
            if hotspot_client and mac_address:
                HotspotClientDevice.record_device(
                    client=hotspot_client,
                    mac_address=mac_address,
                )

            # ══════════════════════════════════════════════════════════════
            # DETERMINE ACCESS CODE - FIXED FOR MULTIPLE DEVICES
            # ══════════════════════════════════════════════════════════════
            if tv_code and reserved_access_code:
                # TV pairing: the TV code itself was pre-reserved as the access_code
                friendly_username = reserved_access_code
                is_roaming = False
                roamed_from_name = None
                logger.info(f"📺 TV PURCHASE: Using reserved access code {friendly_username}")

            elif hotspot_client and hotspot_client.canonical_username:
                base_username = hotspot_client.canonical_username
                
                # Check if this exact MAC already has an active session with the canonical name
                existing_for_this_mac = HotspotSession.objects.filter(
                    hotspot_client=hotspot_client,
                    access_code=base_username,
                    mac_address=mac_address,
                    status__in=('active', 'paid'),
                    expires_at__gt=timezone.now()
                ).exists()
                
                if existing_for_this_mac:
                    # Same device reconnecting — reuse canonical name (auto-login case)
                    friendly_username = base_username
                    is_roaming = False
                    roamed_from_name = None
                    logger.info(f"🔄 RE-CONNECT: Same device {mac_address} using {friendly_username}")
                else:
                    # Different device for same client — assign a device-specific slot
                    active_count = HotspotSession.objects.filter(
                        hotspot_client=hotspot_client,
                        status__in=('active', 'paid'),
                        expires_at__gt=timezone.now()
                    ).exclude(mac_address=mac_address).count()
                    
                    if active_count == 0:
                        # No other active sessions — use canonical name
                        friendly_username = base_username
                        is_roaming = False
                        roamed_from_name = None
                        logger.info(f"🏠 FIRST DEVICE: {friendly_username} at {router.name}")
                    else:
                        # Other devices already using this client's slots
                        # Issue a numbered device credential: MXA-BKCS-2, MXA-BKCS-3, etc.
                        device_slot = active_count + 1
                        friendly_username = f"{base_username}-{device_slot}"
                        # Ensure uniqueness (collision guard)
                        while HotspotSession.objects.filter(
                            access_code=friendly_username,
                            status__in=('active', 'paid'),
                            expires_at__gt=timezone.now()
                        ).exists():
                            device_slot += 1
                            friendly_username = f"{base_username}-{device_slot}"
                        
                        is_roaming = False
                        roamed_from_name = None
                        logger.info(f"📱 ADDITIONAL DEVICE: {friendly_username} (slot {device_slot}) at {router.name}")

                # Roaming detection: did they connect here from a different router?
                prev_session = (
                    HotspotSession.objects
                    .filter(hotspot_client=hotspot_client, access_code=friendly_username)
                    .exclude(router=router)
                    .order_by("-created_at")
                    .first()
                )
                if prev_session:
                    is_roaming = True
                    roamed_from_name = prev_session.router.name if prev_session.router else None
                    logger.info(f"📍 ROAMING: {friendly_username} moved from {roamed_from_name} → {router.name}")

            else:
                # Fallback: generate a one-off code (should rarely happen given
                # get_or_create_by_mac always sets canonical_username)
                friendly_username = self.generate_unique_code()
                is_roaming = False
                roamed_from_name = None
                logger.warning(
                    f"⚠️  No canonical_username for client, using one-off code "
                    f"{friendly_username} for MAC {mac_address}"
                )

            session_id = HotspotSession.generate_session_id()
            session = HotspotSession.objects.create(
                session_id=session_id,
                router=router,
                plan=plan,
                phone_number=phone_number,
                mac_address=mac_address,
                amount=plan.price,
                status='pending',
                access_code=friendly_username,
                is_roaming=is_roaming,
                roamed_from=roamed_from_name,
                hotspot_client=hotspot_client,  # Link to transient client
            )

            # Resolve active/default tenant payment method (MPESA_STK)
            payment_method = self._get_active_hotspot_payment_method(tenant.schema_name)
            if not payment_method:
                session.mark_failed("No active M-Pesa payment method configured")
                return Response(
                    {
                        'error': (
                            'No active M-Pesa payment method configured. '
                            'Please set up a payment method in the admin dashboard under '
                            'Billing → Payment Methods.'
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            payment_ref = f"HS-{session.session_id}-{int(time.time())}".replace(" ", "-")

            # Create payment linked to selected method
            payment = Payment.objects.create(
                customer=None,
                payment_method=payment_method,
                amount=plan.price,
                transaction_fee=0,
                net_amount=plan.price,
                currency='KES',
                status='PROCESSING',
                payment_reference=payment_ref,
                payer_phone=phone_number,
                schema_name=tenant.schema_name,
                hotspot_session=session,
                tuma_status='pending',
            )

            # ===============================
            # Provider branch: Daraja (own keys) first, else Tuma
            # ===============================
            try:
                # Branch 1: Tenant's own Daraja credentials (MpesaConfiguration)
                # This is the ONLY case where we bypass Tuma
                if (payment_method.mpesa_configuration and 
                    payment_method.mpesa_configuration.is_active):
                    mpesa_cfg = payment_method.mpesa_configuration
                    self._ensure_mpesa_stk_callback_url(mpesa_cfg)

                    mpesa_service = MpesaSTKPush(config=mpesa_cfg)
                    mpesa_result = mpesa_service.initiate_stk_push(
                        phone_number=phone_number,
                        amount=plan.price,
                        account_reference=session.session_id[:12],
                        transaction_desc="Hotspot Access",
                        payment=payment,
                    )

                    if not mpesa_result.get('success'):
                        payment.status = 'FAILED'
                        payment.failure_reason = mpesa_result.get('message', 'M-Pesa STK initiation failed')
                        payment.save(update_fields=['status', 'failure_reason'])
                        session.mark_failed(payment.failure_reason)
                        return Response({'error': payment.failure_reason}, status=status.HTTP_400_BAD_REQUEST)

                    d = mpesa_result.get('data', {})
                    session.tuma_merchant_request_id = d.get('merchant_request_id', '')
                    session.tuma_checkout_request_id = d.get('checkout_request_id', '')
                    session.payment = payment
                    session.save(update_fields=['tuma_merchant_request_id', 'tuma_checkout_request_id', 'payment'])

                else:
                    # Branch 2: Tuma — resolve config from method FK first,
                    # then fall back to tenant-level TenantTumaConfig
                    cfg = None
                    
                    if (payment_method.tuma_configuration and 
                        payment_method.tuma_configuration.is_active):
                        cfg = payment_method.tuma_configuration
                    else:
                        # Fallback: look up TenantTumaConfig directly by schema
                        try:
                            cfg = TenantTumaConfig.objects.get(
                                schema_name=tenant.schema_name,
                                is_active=True
                            )
                        except TenantTumaConfig.DoesNotExist:
                            cfg = None
                    
                    if not cfg:
                        payment.status = 'FAILED'
                        payment.failure_reason = "No payment gateway configured (Tuma not set up)"
                        payment.save(update_fields=['status', 'failure_reason'])
                        session.mark_failed(payment.failure_reason)
                        return Response(
                            {
                                'error': (
                                    'No active payment gateway configured. '
                                    'Please set up a payment method in the admin dashboard under '
                                    'Billing → Payment Methods.'
                                )
                            },
                            status=status.HTTP_400_BAD_REQUEST
                        )

                    if not cfg.tuma_business_email or not cfg.tuma_business_api_key:
                        payment.status = 'FAILED'
                        payment.failure_reason = "Tuma gateway credentials missing"
                        payment.save(update_fields=['status', 'failure_reason'])
                        session.mark_failed(payment.failure_reason)
                        return Response({'error': payment.failure_reason}, status=status.HTTP_400_BAD_REQUEST)

                    client = TumaClient()
                    token = client.get_token(cfg.tuma_business_email, cfg.tuma_business_api_key)
                    description = f"HS-{session.session_id}"

                    callback_url = getattr(settings, 'TUMA_CALLBACK_URL', None)
                    if not callback_url:
                        callback_url = f"https://{tenant_subdomain}.netily.co.ke/api/v1/billing/tuma/callback/"

                    tuma_res = client.stk_push(
                        token=token,
                        amount=float(plan.price),
                        phone=phone_number,
                        callback_url=callback_url,
                        description=description,
                    )

                    if not tuma_res.get("success"):
                        payment.status = 'FAILED'
                        payment.tuma_status = 'failed'
                        payment.failure_reason = tuma_res.get("message", "STK initiation failed")
                        payment.save(update_fields=['status', 'tuma_status', 'failure_reason'])
                        session.mark_failed(payment.failure_reason)
                        return Response({'error': payment.failure_reason}, status=status.HTTP_400_BAD_REQUEST)

                    d = tuma_res.get("data", {})
                    payment.tuma_merchant_request_id = d.get("merchant_request_id", "")
                    payment.tuma_checkout_request_id = d.get("checkout_request_id", "")
                    payment.save(update_fields=['tuma_merchant_request_id', 'tuma_checkout_request_id'])

                    with schema_context(get_public_schema_name()):
                        TumaCallbackMap.objects.update_or_create(
                            merchant_request_id=payment.tuma_merchant_request_id,
                            defaults={
                                "checkout_request_id": payment.tuma_checkout_request_id,
                                "schema_name": tenant.schema_name,
                                "payment_reference": payment.payment_number,
                            },
                        )

                    session.tuma_merchant_request_id = payment.tuma_merchant_request_id
                    session.tuma_checkout_request_id = payment.tuma_checkout_request_id
                    session.payment = payment
                    session.save(update_fields=['tuma_merchant_request_id', 'tuma_checkout_request_id', 'payment'])

                logger.info(
                    f"STK Push initiated for session {session.session_id}, "
                    f"payment {payment.payment_number}, method={payment_method.code}"
                )

            except Exception as e:
                err_text = str(e)
                logger.error(f"STK initiation failed for session {session.session_id}: {err_text}", exc_info=True)

                retriable_markers = ["404", "429", "502", "503", "504", "timed out", "connection", "temporar"]
                is_retriable = any(m in err_text.lower() for m in retriable_markers)

                if is_retriable:
                    payment.failure_reason = err_text[:250]
                    payment.save(update_fields=['failure_reason'])

                    return Response(
                        {
                            "error": "Payment gateway is temporarily unavailable. Please retry in 5-10 seconds.",
                            "retriable": True,
                        },
                        status=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )

                payment.status = 'FAILED'
                payment.tuma_status = 'failed'
                payment.failure_reason = err_text
                payment.save(update_fields=['status', 'tuma_status', 'failure_reason'])
                session.mark_failed(payment.failure_reason)

                return Response(
                    {
                        "error": "Failed to initiate payment. Please confirm your number and try again.",
                        "retriable": False,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # ============================================================
            # SMS: new subscription initiated (after STK push succeeds)
            # ============================================================
            try:
                from apps.messaging.services.notification_sender import SMSNotifier
                SMSNotifier.hotspot_new_subscription(session)
            except Exception as e:
                logger.warning(f"Hotspot new-subscription SMS failed: {e}")

            # ============================================================
            # OPTIONALLY invalidate used TV code after successful payment init
            # ============================================================
            if tv_code:
                try:
                    tv_cache_key = f"tv_code:{tenant.schema_name}:{tv_code}"
                    cache.delete(tv_cache_key)
                    device_cache_key = f"tv_device:{tenant.schema_name}:{router_id}:{mac_address}"
                    cache.delete(device_cache_key)
                    logger.info(f"TV code {tv_code} invalidated after purchase")
                except Exception as e:
                    logger.warning(f"Failed to delete TV code {tv_code}: {e}")

            return Response({
                'status': 'pending',
                'session_id': session.session_id,
                'message': 'STK push sent to your phone. Please complete payment on your M-Pesa.',
                'payment_reference': payment.payment_number,
                'amount': float(plan.price),
                'phone_number': phone_number,
            }, status=status.HTTP_202_ACCEPTED)


class HotspotPurchaseStatusView(APIView):
    """
    Poll hotspot purchase status.
    
    PUBLIC ENDPOINT - No authentication required.
    
    GET /api/v1/hotspot/purchase/{session_id}/status/
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []
    
    def _check_tuma_payment_status(self, session, tenant_schema):
        payment = None
        
        if getattr(session, 'payment_id', None):
            try:
                payment = Payment.objects.filter(id=session.payment_id).first()
                if payment:
                    logger.debug(f"Found payment via explicit FK: {payment.payment_number}")
            except Exception as e:
                logger.warning(f"Error fetching payment by FK: {e}")
        
        if not payment:
            if hasattr(session, 'tuma_checkout_request_id') and session.tuma_checkout_request_id:
                payment = Payment.objects.filter(
                    tuma_checkout_request_id=session.tuma_checkout_request_id
                ).first()
                if payment:
                    logger.debug(f"Found payment via checkout_request_id: {session.tuma_checkout_request_id}")
            
            if not payment and hasattr(session, 'tuma_merchant_request_id') and session.tuma_merchant_request_id:
                payment = Payment.objects.filter(
                    tuma_merchant_request_id=session.tuma_merchant_request_id
                ).first()
                if payment:
                    logger.debug(f"Found payment via merchant_request_id: {session.tuma_merchant_request_id}")
        
        if not payment:
            payments = Payment.objects.filter(
                payer_phone=session.phone_number,
                amount=session.amount,
                status__in=['PROCESSING', 'COMPLETED', 'FAILED']
            ).order_by('-created_at')
            payment = payments.first()
            if payment:
                logger.debug(f"Found payment via phone+amount fallback: {payment.payment_number}")
        
        if not payment:
            return ('pending', 'No payment record found', None)
        
        if payment.status == 'COMPLETED':
            return ('completed', 'Payment successful', payment)
        elif payment.status == 'FAILED':
            return ('failed', payment.failure_reason or 'Payment failed', None)
        else:
            if payment.tuma_status == 'completed' or str(payment.tuma_result_code) == '0':
                if payment.status == 'PROCESSING':
                    payment.status = 'COMPLETED'
                    payment.save()
                return ('completed', 'Payment successful', payment)
            elif payment.tuma_status == 'failed' or (payment.tuma_result_code and str(payment.tuma_result_code) != '0'):
                return ('failed', payment.tuma_result_desc or 'Payment failed', None)
            else:
                return ('pending', 'Waiting for payment confirmation...', None)
    
    @transaction.atomic
    def get(self, request, session_id):
        tenant_subdomain = request.query_params.get('tenant')
        if not tenant_subdomain:
            return Response({'error': 'Tenant is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from apps.core.models import Tenant
            with schema_context(get_public_schema_name()):
                tenant = Tenant.objects.get(subdomain=tenant_subdomain, is_active=True)
        except Exception:
            return Response({'error': 'Invalid tenant'}, status=status.HTTP_400_BAD_REQUEST)

        with schema_context(tenant.schema_name):
            try:
                session = HotspotSession.objects.get(session_id=session_id)
            except HotspotSession.DoesNotExist:
                return Response(
                    {'error': 'Session not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            if session.status == 'active':
                return Response({
                    'status': 'success',
                    'message': 'Payment received! You are now connected.',
                    'access_code': session.access_code,
                    'expires_at': session.expires_at,
                    'duration_display': session.plan.duration_display,
                    'data_remaining_mb': session.data_remaining_mb,
                    'speed': f"{session.plan.speed_limit_mbps}Mbps",
                    'login_url': request.query_params.get('login_url', ''),
                })
            
            elif session.status == 'failed':
                return Response({
                    'status': 'failed',
                    'message': session.failure_reason or 'Payment failed. Please try again.',
                })
            
            elif session.status == 'expired':
                return Response({
                    'status': 'expired',
                    'message': 'Session has expired.',
                })
            
            elif session.status == 'paid':
                session.activate(session.access_code)
                
                # ────────────────────────────────────────────────────────────
                # FIX 2: Close only prior radacct rows for renewal
                # ────────────────────────────────────────────────────────────
                closed_count = _close_prior_radacct_rows_for_renewal(session, session.access_code)
                if closed_count > 0:
                    logger.info(
                        f"Closed {closed_count} old RADIUS sessions for {session.access_code} before renewal activation"
                    )
                
                try:
                    from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                    
                    radius_service = HotspotRadiusService()
                    radius_service.create_hotspot_credentials(
                        username=session.access_code,
                        password=session.access_code,
                        router=session.router,
                        plan=session.plan,
                        expires_at=session.expires_at,
                        mac_address=session.mac_address or '',
                    )
                except Exception as e:
                    logger.error(f"RADIUS activation failed for paid session {session.session_id}: {e}")
                
                # ── SMS: welcome with access code ──
                try:
                    from apps.messaging.services.notification_sender import SMSNotifier
                    SMSNotifier.hotspot_welcome(session)
                except Exception as e:
                    logger.warning(f"Hotspot welcome SMS failed: {e}")
                
                return Response({
                    'status': 'success',
                    'message': 'Payment received! You are now connected.',
                    'access_code': session.access_code,
                    'expires_at': session.expires_at,
                    'duration_display': session.plan.duration_display,
                    'data_remaining_mb': session.data_remaining_mb,
                    'speed': f"{session.plan.speed_limit_mbps}Mbps",
                    'login_url': request.query_params.get('login_url', ''),
                })
            
            if session.phone_number:
                status, message, payment = self._check_tuma_payment_status(session, tenant.schema_name)
                
                if status == 'completed':
                    mpesa_receipt = payment.mpesa_receipt if payment else ''
                    session.mark_paid(mpesa_receipt)
                    
                    if payment:
                        session.tuma_checkout_request_id = payment.tuma_checkout_request_id
                        session.tuma_merchant_request_id = payment.tuma_merchant_request_id
                        session.payment = payment
                        session.save(update_fields=['tuma_checkout_request_id', 'tuma_merchant_request_id', 'payment'])
                    
                    session.activate(session.access_code)
                    
                    # ────────────────────────────────────────────────────────────
                    # FIX 2: Close only prior radacct rows for renewal
                    # ────────────────────────────────────────────────────────────
                    closed_count = _close_prior_radacct_rows_for_renewal(session, session.access_code)
                    if closed_count > 0:
                        logger.info(
                            f"Closed {closed_count} old RADIUS sessions for {session.access_code} before renewal activation"
                        )
                    
                    try:
                        from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                        
                        radius_service = HotspotRadiusService()
                        radius_service.create_hotspot_credentials(
                            username=session.access_code,
                            password=session.access_code,
                            router=session.router,
                            plan=session.plan,
                            expires_at=session.expires_at,
                            mac_address=session.mac_address or '',
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to create RADIUS credentials for session "
                            f"{session.session_id}: {e}",
                            exc_info=True
                        )
                    
                    # ── SMS: welcome ──
                    try:
                        from apps.messaging.services.notification_sender import SMSNotifier
                        SMSNotifier.hotspot_welcome(session)
                    except Exception as e:
                        logger.warning(f"Hotspot welcome SMS failed: {e}")
                    
                    return Response({
                        'status': 'success',
                        'message': 'Payment received! You are now connected.',
                        'access_code': session.access_code,
                        'expires_at': session.expires_at,
                        'duration_display': session.plan.duration_display,
                        'data_remaining_mb': session.data_remaining_mb,
                        'speed': f"{session.plan.speed_limit_mbps}Mbps",
                    })
                
                elif status == 'failed':
                    session.mark_failed(message)
                    # ── SMS: payment failed ──
                    try:
                        from apps.messaging.services.notification_sender import SMSNotifier
                        SMSNotifier.hotspot_payment_failed(session, message)
                    except Exception as e:
                        logger.warning(f"Hotspot failed SMS error: {e}")
                    return Response({
                        'status': 'failed',
                        'message': message or 'Payment failed. Please try again.',
                    })
                
                elif status == 'pending':
                    logger.debug(f"Hotspot payment pending for session {session_id}: {message}")
            
            return Response({
                'status': 'pending',
                'message': 'Waiting for payment confirmation on your phone...',
                'session_id': session.session_id,
            })


class HotspotVoucherRedeemView(APIView):
    """
    Redeem a voucher code on the hotspot captive portal.

    PUBLIC ENDPOINT - No authentication required.
    The user enters a voucher code (and optional PIN) received from the ISP.
    If valid, a hotspot session is created and RADIUS credentials returned.

    POST /api/v1/hotspot/voucher-redeem/
    {
        "code": "ABC123",
        "router_id": 5,           # Still required to know which router to connect to
        "mac_address": "AA:BB:CC:DD:EE:FF",
        "tenant": "indigo3"
    }
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def _generate_code(self):
        chars = string.ascii_uppercase + string.digits
        while True:
            part1 = ''.join(random.choices(chars, k=4))
            part2 = ''.join(random.choices(chars, k=4))
            code = f"{part1}-{part2}"
            if not HotspotSession.objects.filter(access_code=code).exists():
                return code

    @transaction.atomic
    def post(self, request):
        tenant_subdomain = request.data.get('tenant') or request.query_params.get('tenant')
        if not tenant_subdomain:
            return Response({'error': 'Tenant is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from apps.core.models import Tenant
            with schema_context(get_public_schema_name()):
                tenant = Tenant.objects.get(subdomain=tenant_subdomain, is_active=True)
        except Exception:
            return Response({'error': 'Invalid tenant'}, status=status.HTTP_400_BAD_REQUEST)

        with schema_context(tenant.schema_name):
            voucher_code = (request.data.get('code') or '').strip()
            router_id = request.data.get('router_id')
            mac_address = (request.data.get('mac_address') or '00:00:00:00:00:00').upper().replace('-', ':')

            if not voucher_code:
                return Response({'error': 'Voucher code is required'}, status=status.HTTP_400_BAD_REQUEST)
            if not router_id:
                return Response({'error': 'Router ID is required'}, status=status.HTTP_400_BAD_REQUEST)

            # ============================================================
            # FIX: Auto-detect plan from voucher - no plan_id required!
            # ============================================================
            try:
                voucher = Voucher.objects.select_related('batch', 'batch__hotspot_plan').get(code__iexact=voucher_code)
            except Voucher.DoesNotExist:
                return Response({'error': 'Invalid voucher code'}, status=status.HTTP_404_NOT_FOUND)

            # Check if voucher is expired or used
            if not voucher.is_valid():
                if voucher.status == 'EXPIRED' or (voucher.valid_to and voucher.valid_to < timezone.now()):
                    reason = 'Voucher has expired'
                elif voucher.status in ('USED', 'REDEEMED') or voucher.use_count >= (voucher.max_uses or 1):
                    reason = 'Voucher has already been used'
                else:
                    reason = 'Voucher is not available'
                return Response({'error': reason}, status=status.HTTP_400_BAD_REQUEST)

            # ============================================================
            # Auto-detect the plan from the voucher
            # ============================================================
            plan = None
            # First check if voucher has hotspot_plan directly
            if hasattr(voucher, 'hotspot_plan') and voucher.hotspot_plan:
                plan = voucher.hotspot_plan
            # Then check the batch's hotspot_plan
            elif voucher.batch and voucher.batch.hotspot_plan:
                plan = voucher.batch.hotspot_plan
            
            if not plan:
                return Response(
                    {'error': 'This voucher is not linked to a valid plan'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Verify the plan is active
            if not plan.is_active:
                return Response(
                    {'error': 'The plan linked to this voucher is no longer available'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Get the router
            try:
                router = Router.objects.get(id=router_id, is_active=True)
            except (Router.DoesNotExist, ValueError):
                try:
                    router = Router.objects.get(name=router_id, is_active=True)
                except Router.DoesNotExist:
                    return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)

            # ============================================================
            # FIX: Enforce voucher plan restriction (if voucher is plan-restricted)
            # ============================================================
            voucher_plan_id = getattr(voucher.batch, 'hotspot_plan_id', None)
            if voucher_plan_id and str(voucher_plan_id) != str(plan.id):
                logger.warning(
                    f"Voucher {voucher.code} is restricted to plan {voucher_plan_id} "
                    f"but the linked plan is {plan.id}"
                )
                return Response(
                    {'error': 'This voucher is not valid for the selected plan'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Check if voucher has sufficient balance
            if voucher.remaining_value is not None and voucher.remaining_value < plan.price:
                return Response({
                    'error': f'Voucher balance (KES {voucher.remaining_value}) is insufficient for this plan (KES {plan.price})'
                }, status=status.HTTP_400_BAD_REQUEST)

            # ══════════════════════════════════════════════════════════════
            # RESOLVE HOTSPOT IDENTITY FOR VOUCHER (using new canonical_username)
            # ══════════════════════════════════════════════════════════════
            from apps.billing.models.hotspot_models import HotspotClient, HotspotClientDevice
            
            # Use MAC as a fallback identifier if real phone isn't provided
            provided_phone = request.data.get('phone_number') or ''
            # MAC-derived fallback must stay within max_length=15: "MAC-" (4) + 11 chars = 15
            phone_to_use = provided_phone if provided_phone else f"MAC-{mac_address.replace(':', '')[:11]}"
            
            # Resolve the persistent client record
            hotspot_client = HotspotClient.get_or_create_by_mac(
                schema_name=tenant.schema_name,
                mac_address=mac_address,
                phone_number=phone_to_use,
            )
            
            if hotspot_client and mac_address:
                HotspotClientDevice.record_device(
                    client=hotspot_client, 
                    mac_address=mac_address
                )
            
            # Use the client's permanent canonical_username as the access code
            if hotspot_client and hotspot_client.canonical_username:
                friendly_username = hotspot_client.canonical_username
                logger.info(f"🔄 VOUCHER: Using permanent username {friendly_username} for {mac_address}")
            else:
                # Fallback: generate new code (should rarely happen)
                friendly_username = self._generate_code()
                logger.warning(f"⚠️ VOUCHER: No canonical_username, using generated code {friendly_username}")

            # Mark voucher as used
            voucher.use_count = (voucher.use_count or 0) + 1
            if voucher.remaining_value is not None:
                voucher.remaining_value = max(Decimal('0'), voucher.remaining_value - plan.price)
            if not voucher.is_reusable or voucher.use_count >= (voucher.max_uses or 1):
                voucher.status = 'USED'
            voucher.save()

            # Create hotspot session
            session_id = HotspotSession.generate_session_id()
            session = HotspotSession.objects.create(
                session_id=session_id,
                router=router,
                plan=plan,
                phone_number='VOUCHER',
                mac_address=mac_address,
                amount=plan.price,
                status='paid',
                access_code=friendly_username,
                payhero_checkout_id=f'VOUCHER_{voucher.code}',
                hotspot_client=hotspot_client,  # Link to persistent client
            )

            try:
                session.activate(friendly_username)
                
                # ────────────────────────────────────────────────────────────
                # FIX 2: Close only prior radacct rows for renewal
                # ────────────────────────────────────────────────────────────
                closed_count = _close_prior_radacct_rows_for_renewal(session, friendly_username)
                if closed_count > 0:
                    logger.info(
                        f"Closed {closed_count} old RADIUS sessions for {friendly_username} before renewal activation"
                    )
                
                from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                radius_service = HotspotRadiusService()
                radius_service.create_hotspot_credentials(
                    username=friendly_username,
                    password=friendly_username,
                    router=session.router,
                    plan=session.plan,
                    expires_at=session.expires_at,
                    mac_address=mac_address,
                )
                logger.info(f"VOUCHER REDEEM: {voucher.code} -> user {friendly_username} at {router.name} (plan: {plan.name})")
                
                # ── SMS: welcome for voucher redemption ──
                try:
                    from apps.messaging.services.notification_sender import SMSNotifier
                    SMSNotifier.hotspot_welcome(session)
                except Exception as e:
                    logger.warning(f"Hotspot voucher welcome SMS failed: {e}")
                    
            except Exception as e:
                logger.error(f"RADIUS activation failed for voucher: {e}")
                return Response({'error': 'Activation failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            return Response({
                'status': 'success',
                'message': f'Voucher redeemed! You are connected with {plan.name} plan.',
                'access_code': friendly_username,
                'username': friendly_username,
                'password': friendly_username,
                'expires_at': session.expires_at,
                'plan_name': plan.name,
                'remaining_voucher_value': str(voucher.remaining_value) if voucher.remaining_value is not None else None,
            })