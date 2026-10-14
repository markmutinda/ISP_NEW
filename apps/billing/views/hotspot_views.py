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
from django.core.cache import cache

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from django_tenants.utils import schema_context, get_public_schema_name

from apps.billing.models.hotspot_models import (
    HotspotPlan, HotspotSession, HotspotBranding,
    HotspotClient, HotspotClientDevice, HotspotFreeTrialUsage
)
from apps.billing.models.billing_models import Plan
from apps.billing.models.payment_models import (
    Payment, TenantTumaConfig, InvoiceItemPayment, StkCancellationTracker
)
from apps.billing.models.voucher_models import Voucher
from apps.billing.services.tuma_service import TumaClient
from apps.billing.integrations.mpesa_integration import MpesaSTKPush
from apps.billing.services.netily_paybill_service import (
    resolve_destination, stk_push, NetilyPaybillError,
)
from apps.billing.utils.payment_errors import humanize_payment_failure
from apps.core.models import TumaCallbackMap
from apps.network.models.router_models import Router
from apps.subscriptions.models import CommissionLedger
from apps.radius.services.coa_service import CoAService

# ── NEW: Hotspot credential reconnect helpers ──
import hmac
import re  # (skip if already imported)

from apps.billing.services.hotspot_credentials import (
    SLOT_CODE_RE, device_limit, device_password,
    schedule_tv_login_push,
)
from apps.core.tenant_cache import resolve_tenant_cached

logger = logging.getLogger(__name__)


MPESA_CAPABLE_METHOD_TYPES = [
    'MPESA_STK', 'MPESA_PAYBILL', 'MPESA_TILL', 'MOBILE_MONEY', 'MPESA',
]


# ============================================================
# HELPER UTILITIES
# ============================================================

def _today_valid_day_field() -> str:
    """Return the HotspotPlan boolean field name for today's weekday."""
    weekday_map = {
        0: 'valid_monday',
        1: 'valid_tuesday',
        2: 'valid_wednesday',
        3: 'valid_thursday',
        4: 'valid_friday',
        5: 'valid_saturday',
        6: 'valid_sunday',
    }
    return weekday_map[timezone.now().weekday()]


def _normalize_mac(mac: str) -> str:
    """Normalize MAC address to uppercase with colons."""
    return (mac or "").upper().replace("-", ":").strip()


def _kick_router_slot_after_commit(access_code, router):
    """
    Free the router-side hotspot slot once the surrounding DB transaction commits
    (runs immediately if none is open). No DB access inside, never raises.

    See apps/billing/services/hotspot_radius_service.py::kick_active_session.

    Using transaction.on_commit ensures the MikroTik API call (up to ~10s over
    the VPN) does not hold a PgBouncer session-mode connection open while the
    surrounding @transaction.atomic block in HotspotPhoneReconnectView is still
    active. on_commit still fires before the HTTP response goes out.
    """
    from apps.billing.services.hotspot_radius_service import HotspotRadiusService
    transaction.on_commit(
        lambda c=access_code, r=router: HotspotRadiusService().kick_active_session(c, r)
    )


def _canonical_phone(phone: str, country_code: str = 'KE') -> str:
    from utils.phone import normalize_phone_number
    return normalize_phone_number(phone, country_code)


def _get_tenant_country(tenant) -> str:
    try:
        return tenant.company.country or 'KE'
    except Exception:
        return 'KE'


def _plan_data_limit_display(plan):
    if plan.data_limit is None:
        return 'Unlimited'
    if plan.data_limit >= 1024:
        tb = plan.data_limit / 1024
        return f'{tb:g} TB'
    return f'{plan.data_limit} GB'


def _plan_validity_value(plan):
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
        'is_free_trial': getattr(plan, 'is_free_trial', False),
        'is_tv_plan': getattr(plan, 'is_tv_plan', False),
    }


def _close_prior_radacct_rows_for_renewal(session, username: str):
    from apps.radius.models import RadAcct

    if not username:
        return 0
    if not session.hotspot_client:
        return 0
    if not session.activated_at:
        return 0

    closed = RadAcct.objects.filter(
        username=username,
        acctstoptime__isnull=True,
        acctstarttime__lt=session.activated_at,
    ).update(
        acctstoptime=timezone.now(),
        acctterminatecause='Session-Timeout',
    )

    if closed:
        logger.info(
            "Closed %d prior-period radacct row(s) for %s (new period started %s)",
            closed, username, session.activated_at.isoformat(),
        )

    return closed


class CaptivePortalView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

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

        from apps.core.tenant_cache import resolve_tenant_cached
        from apps.core.cache_versioning import get_cache_version

        tenant_info = resolve_tenant_cached(tenant_subdomain)
        if not tenant_info:
            return Response(
                {'status': 'error', 'message': 'Tenant not found'},
                status=status.HTTP_400_BAD_REQUEST
            )

        schema_name = tenant_info['schema_name']
        version = get_cache_version(schema_name)
        cache_key = f"hotspot:captive:v2:{schema_name}:{router_id}:{version}"

        cached_payload = cache.get(cache_key)
        if cached_payload is not None:
            return Response(cached_payload)

        lock_key = f"{cache_key}:lock"
        if not cache.add(lock_key, "1", timeout=5):
            stale_key = f"hotspot:captive:v2:{schema_name}:{router_id}:{version - 1}"
            stale_payload = cache.get(stale_key)
            if stale_payload is not None:
                return Response(stale_payload)

        try:
            with schema_context(schema_name):
                router_qs = Router.objects.filter(is_active=True).select_related('hotspot_branding')

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
                        'router_logo_url': None,
                        'hide_plan_speed': False,
                        'portal_font': 'outfit',
                    }
                    branding_data = None
                else:
                    router_logo_url = None
                    if getattr(router, 'logo', None):
                        try:
                            router_logo_url = request.build_absolute_uri(router.logo.url)
                        except Exception:
                            pass

                    portal_config = {
                        'template_id': router.template_id or 1,
                        'hotspot_name': router.hotspot_name or router.name,
                        'support_phone': router.support_phone or '',
                        'announcement_text': router.announcement_text or '',
                        'gateway_ip': router.gateway_ip,
                        'router_logo_url': router_logo_url,
                        'hide_plan_speed': getattr(router, 'hide_plan_speed', False),
                        'portal_font': getattr(router, 'portal_font', 'outfit') or 'outfit',
                    }

                    branding_data = None
                    branding = getattr(router, 'hotspot_branding', None)

                    if branding is None:
                        default_branding_key = f"hotspot:default_branding:{schema_name}"
                        branding = cache.get(default_branding_key)
                        if branding is None:
                            branding = HotspotBranding.objects.filter(is_default=True).only(
                                'company_name', 'logo', 'background_image',
                                'primary_color', 'secondary_color', 'text_color', 'background_color',
                                'welcome_title', 'welcome_message', 'support_phone', 'support_email'
                            ).first()
                            cache.set(default_branding_key, branding or False, timeout=3600)
                        elif branding is False:
                            branding = None

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

                    if router and getattr(router, 'logo', None):
                        try:
                            router_logo_url = request.build_absolute_uri(router.logo.url)
                            if branding_data is None:
                                branding_data = {'logo_url': router_logo_url}
                            elif not branding_data.get('logo_url'):
                                branding_data['logo_url'] = router_logo_url
                        except Exception:
                            pass

                plans_data = []
                if router is not None:
                    try:
                        hotspot_plans = HotspotPlan.objects.filter(
                            router=router,
                            is_active=True,
                            **{_today_valid_day_field(): True},
                        ).only(
                            'id', 'name', 'description', 'price', 'currency',
                            'validity_type', 'validity_value',
                            'download_speed', 'upload_speed', 'speed_unit',
                            'limitation_type', 'data_limit_value', 'data_limit_unit',
                            'simultaneous_devices', 'is_popular', 'duration_minutes', 'data_limit_mb',
                            'is_free_trial',
                            'is_tv_plan',
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
        finally:
            cache.delete(lock_key)

        payload = {
            'status': 'success',
            'portal_config': portal_config,
            'branding': branding_data,
            'plans': plans_data,
        }

        cache.set(cache_key, payload, timeout=3600)

        from django.http import JsonResponse
        response = JsonResponse(payload)
        response["Cache-Control"] = "public, max-age=15, stale-while-revalidate=60"
        return response


class HotspotPlansView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, router_id):
        cache_key = f"hotspot:plans:v1:{router_id}"
        cached_payload = cache.get(cache_key)
        if cached_payload is not None:
            from django.http import JsonResponse
            response = JsonResponse(cached_payload)
            response["Cache-Control"] = "public, max-age=15, stale-while-revalidate=60"
            return response

        router_qs = Router.objects.filter(is_active=True).only(
            'id', 'name', 'location', 'template_id', 'hotspot_name',
            'support_phone', 'announcement_text', 'hide_plan_speed', 'portal_font'
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
            is_active=True,
            **{_today_valid_day_field(): True},
        ).only(
            'id', 'name', 'price', 'currency', 'duration_minutes', 'data_limit_mb',
            'download_speed', 'upload_speed', 'speed_unit', 'description',
            'is_popular', 'validity_type', 'validity_value',
            'limitation_type', 'data_limit_value', 'data_limit_unit',
            'is_free_trial',
            'is_tv_plan',
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
                'is_free_trial': plan.is_free_trial,
                'is_tv_plan': plan.is_tv_plan,
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

        router_logo_url = None
        if getattr(router, 'logo', None):
            try:
                router_logo_url = request.build_absolute_uri(router.logo.url)
                if branding_data is None:
                    branding_data = {'logo_url': router_logo_url}
                elif not branding_data.get('logo_url'):
                    branding_data['logo_url'] = router_logo_url
            except Exception:
                pass

        portal_config = {
            'template_id': router.template_id or 1,
            'hotspot_name': router.hotspot_name or router.name,
            'support_phone': router.support_phone or (branding.support_phone if branding else ''),
            'announcement_text': router.announcement_text or '',
            'router_logo_url': router_logo_url,
            'hide_plan_speed': getattr(router, 'hide_plan_speed', False),
            'portal_font': getattr(router, 'portal_font', 'outfit') or 'outfit',
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

        from django.http import JsonResponse
        response = JsonResponse(payload)
        response["Cache-Control"] = "public, max-age=15, stale-while-revalidate=60"
        return response


class HotspotPurchaseView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def generate_unique_code(self):
        chars = string.ascii_uppercase + string.digits

        while True:
            part1 = ''.join(random.choices(chars, k=4))
            part2 = ''.join(random.choices(chars, k=4))
            code = f"{part1}-{part2}"

            if not HotspotSession.objects.filter(access_code=code).exists():
                return code

    def _get_active_hotspot_payment_method(self, schema_name: str):
        daraja_method = (
            InvoiceItemPayment.objects
            .filter(
                schema_name=schema_name,
                is_active=True,
                mpesa_configuration__isnull=False,
                mpesa_configuration__is_active=True,
            )
            .select_related('mpesa_configuration', 'tuma_configuration')
            .order_by('-is_default', '-updated_at')
            .first()
        )
        if daraja_method:
            logger.info(
                f"[{schema_name}] Hotspot gateway → Daraja "
                f"(shortcode={daraja_method.mpesa_configuration.business_shortcode})"
            )
            return daraja_method

        from apps.billing.services.netily_paybill_service import resolve_destination

        candidates = (
            InvoiceItemPayment.objects
            .filter(schema_name=schema_name, is_active=True)
            .filter(mpesa_configuration__isnull=True)
            .order_by('-is_default', '-updated_at')
        )
        for method in candidates:
            if resolve_destination(method):
                logger.info(
                    f"[{schema_name}] Hotspot gateway → Netily Paybill "
                    f"(code={method.code}, type={method.method_type})"
                )
                return method

        return None

    def _ensure_mpesa_stk_callback_url(self, mpesa_cfg):
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

            customer_country = _get_tenant_country(tenant)

            reserved_access_code = None

            if tv_code:
                tv_cache_key = f"tv_code:{tenant.schema_name}:{tv_code}"
                tv_payload = cache.get(tv_cache_key)

                if not tv_payload:
                    return Response(
                        {'error': 'Invalid or expired TV code. Please refresh the TV screen and try again.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                mac_address = tv_payload.get('mac_address', '')
                router_id_from_code = tv_payload.get('router_id')

                if router_id_from_code:
                    router_id = router_id_from_code

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

            phone_canonical = _canonical_phone(phone_number, customer_country)

            stk_tracker = StkCancellationTracker.get_or_create_tracker(
                tenant.schema_name, phone_canonical
            )
            if stk_tracker.is_currently_blocked():
                return Response(
                    {
                        'error': 'Too many cancelled payment prompts. Please wait a few minutes and try again.',
                        'retriable': True,
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS
                )

            from apps.billing.models.hotspot_models import HotspotClient, HotspotClientDevice

            hotspot_client = HotspotClient.get_or_create_by_mac(
                schema_name=tenant.schema_name,
                mac_address=mac_address,
                phone_number=phone_canonical,
            )

            if hotspot_client and mac_address:
                HotspotClientDevice.record_device(
                    client=hotspot_client,
                    mac_address=mac_address,
                )

            if tv_code and reserved_access_code:
                friendly_username = reserved_access_code
                is_roaming = False
                roamed_from_name = None
                logger.info(f"📺 TV PURCHASE: Using reserved access code {friendly_username}")

            elif hotspot_client and hotspot_client.canonical_username:
                base_username = hotspot_client.canonical_username

                existing_for_this_mac = HotspotSession.objects.filter(
                    hotspot_client=hotspot_client,
                    access_code=base_username,
                    mac_address=mac_address,
                    status__in=('active', 'paid'),
                    expires_at__gt=timezone.now()
                ).exists()

                if existing_for_this_mac:
                    friendly_username = base_username
                    is_roaming = False
                    roamed_from_name = None
                    logger.info(f"🔄 RE-CONNECT: Same device {mac_address} using {friendly_username}")
                else:
                    active_count = HotspotSession.objects.filter(
                        hotspot_client=hotspot_client,
                        status__in=('active', 'paid'),
                        expires_at__gt=timezone.now()
                    ).exclude(mac_address=mac_address).count()

                    if active_count == 0:
                        friendly_username = base_username
                        is_roaming = False
                        roamed_from_name = None
                        logger.info(f"🏠 FIRST DEVICE: {friendly_username} at {router.name}")
                    else:
                        device_slot = active_count + 1
                        friendly_username = f"{base_username}-{device_slot}"
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
                phone_number=phone_canonical,
                mac_address=mac_address,
                amount=plan.price,
                status='pending',
                access_code=friendly_username,
                is_roaming=is_roaming,
                roamed_from=roamed_from_name,
                hotspot_client=hotspot_client,
            )

            if request.data.get('target_device') == 'tv':
                cache.set(
                    f"hs_tv_push:{tenant.schema_name}:{session.session_id}",
                    1,
                    timeout=3600,
                )

            payment_method = self._get_active_hotspot_payment_method(tenant.schema_name)
            if not payment_method:
                session.mark_failed("No active M-Pesa payment method configured")
                return Response(
                    {
                        'error': (
                            'No active payment method configured. '
                            'Please set up a payment method in the admin dashboard under '
                            'Billing → Payment Methods.'
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST
                )

            payment_ref = f"HS-{session.session_id}-{int(time.time())}".replace(" ", "-")

            payment = Payment.objects.create(
                customer=None,
                payment_method=payment_method,
                amount=plan.price,
                transaction_fee=0,
                net_amount=plan.price,
                status='PROCESSING',
                payment_reference=payment_ref,
                payer_phone=phone_canonical,
                schema_name=tenant.schema_name,
                hotspot_session=session,
                tuma_status='pending',
                service_type='HOTSPOT',
            )

            try:
                if (payment_method.mpesa_configuration and 
                    payment_method.mpesa_configuration.is_active):
                    mpesa_cfg = payment_method.mpesa_configuration
                    self._ensure_mpesa_stk_callback_url(mpesa_cfg)

                    mpesa_service = MpesaSTKPush(config=mpesa_cfg)
                    mpesa_result = mpesa_service.initiate_stk_push(
                        phone_number=phone_canonical,
                        amount=plan.price,
                        account_reference=session.session_id[:12],
                        transaction_desc="Hotspot Access",
                        payment=payment,
                    )

                    if not mpesa_result.get('success'):
                        payment.status = 'FAILED'
                        payment.failure_reason = humanize_payment_failure(
                            mpesa_result.get('message', 'M-Pesa STK initiation failed')
                        )
                        payment.save(update_fields=['status', 'failure_reason'])
                        session.mark_failed(payment.failure_reason)
                        return Response({'error': payment.failure_reason}, status=status.HTTP_400_BAD_REQUEST)

                    d = mpesa_result.get('data', {})
                    session.tuma_merchant_request_id = d.get('merchant_request_id', '')
                    session.tuma_checkout_request_id = d.get('checkout_request_id', '')
                    session.payment = payment
                    session.save(update_fields=['tuma_merchant_request_id', 'tuma_checkout_request_id', 'payment'])

                else:
                    destination = resolve_destination(payment_method)
                    if not destination:
                        payment.status = 'FAILED'
                        payment.failure_reason = "No valid settlement destination configured"
                        payment.save(update_fields=['status', 'failure_reason'])
                        session.mark_failed(payment.failure_reason)
                        return Response(
                            {'error': 'No active payment gateway configured. Please contact support.'},
                            status=status.HTTP_400_BAD_REQUEST
                        )

                    party_b, account_reference, transaction_type, _desc = destination

                    try:
                        result = stk_push(
                            amount=plan.price,
                            phone_number=phone_canonical,
                            party_b=party_b,
                            account_reference=account_reference or session.session_id[:12],
                            transaction_desc=f"HS-{session.session_id}"[:13],
                            transaction_type=transaction_type,
                        )
                    except NetilyPaybillError as e:
                        payment.status = 'FAILED'
                        payment.tuma_status = 'failed'
                        payment.failure_reason = humanize_payment_failure(str(e))
                        payment.save(update_fields=['status', 'tuma_status', 'failure_reason'])
                        session.mark_failed(payment.failure_reason)

                        try:
                            from apps.notifications.tasks import send_telegram_payment_alert_task
                            from apps.core.telegram_notify import build_payment_failure_message
                            tenant_label = tenant.company.name if hasattr(tenant, 'company') and tenant.company else tenant.subdomain
                            send_telegram_payment_alert_task.apply_async(args=[
                                build_payment_failure_message(
                                    phone=phone_canonical,
                                    amount=plan.price,
                                    tenant_label=tenant_label,
                                    reason=payment.failure_reason,
                                )
                            ], retry=False)
                        except Exception as alert_err:
                            logger.warning(f"Telegram failure alert enqueue failed: {alert_err}")

                        return Response({'error': payment.failure_reason}, status=status.HTTP_400_BAD_REQUEST)

                    payment.tuma_merchant_request_id = result['merchant_request_id']
                    payment.tuma_checkout_request_id = result['checkout_request_id']
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

                is_403 = "403" in err_text or "blacklisted" in err_text.lower()

                log_fn = logger.warning if is_403 else logger.error
                log_fn(
                    f"STK initiation failed for session {session.session_id}: {err_text}",
                    exc_info=not is_403,
                )

                retriable_markers = ["404", "429", "502", "503", "504", "timed out", "connection", "temporar"]
                is_retriable = any(m in err_text.lower() for m in retriable_markers)

                if is_retriable:
                    payment.failure_reason = humanize_payment_failure(err_text)[:250]
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
                payment.failure_reason = humanize_payment_failure(err_text)
                payment.save(update_fields=['status', 'tuma_status', 'failure_reason'])
                session.mark_failed(payment.failure_reason)

                return Response(
                    {
                        "error": "Failed to initiate payment. Please confirm your number and try again.",
                        "retriable": False,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

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
                'phone_number': phone_canonical,
            }, status=status.HTTP_202_ACCEPTED)


class HotspotPurchaseStatusView(APIView):
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
                return ('failed', humanize_payment_failure(payment.tuma_result_desc), None)
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

                    schedule_tv_login_push(session, tenant.schema_name)
                except Exception as e:
                    logger.error(f"RADIUS activation failed for paid session {session.session_id}: {e}")

                from apps.messaging.tasks import send_hotspot_welcome_sms
                send_hotspot_welcome_sms.delay(session.session_id, tenant.schema_name)

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

                        schedule_tv_login_push(session, tenant.schema_name)
                    except Exception as e:
                        logger.error(
                            f"Failed to create RADIUS credentials for session "
                            f"{session.session_id}: {e}",
                            exc_info=True
                        )

                    from apps.messaging.tasks import send_hotspot_welcome_sms
                    send_hotspot_welcome_sms.delay(session.session_id, tenant.schema_name)

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
            is_tv_target = request.data.get('target_device') == 'tv'

            if not voucher_code:
                return Response({'error': 'Voucher code is required'}, status=status.HTTP_400_BAD_REQUEST)
            if not router_id:
                return Response({'error': 'Router ID is required'}, status=status.HTTP_400_BAD_REQUEST)

            existing_session = HotspotSession.objects.filter(
                payhero_checkout_id=f'VOUCHER_{voucher_code}',
                mac_address=mac_address,
                status__in=('active', 'paid'),
            ).select_related('plan').first()

            if existing_session:
                if is_tv_target:
                    cache.set(f"hs_tv_push:{tenant.schema_name}:{existing_session.session_id}", 1, timeout=3600)
                    schedule_tv_login_push(existing_session, tenant.schema_name)
                return Response({
                    'status': 'success',
                    'message': f'Voucher already redeemed! You are connected with {existing_session.plan.name} plan.',
                    'access_code': existing_session.access_code,
                    'username': existing_session.access_code,
                    'password': existing_session.access_code,
                    'expires_at': existing_session.expires_at,
                    'plan_name': existing_session.plan.name,
                    'remaining_voucher_value': None,
                })

            lock_key = f"voucher_redeem_lock:{tenant.schema_name}:{voucher_code}:{mac_address}"
            if not cache.add(lock_key, "1", timeout=15):
                return Response(
                    {'error': 'Redemption already in progress. Please wait a moment.'},
                    status=status.HTTP_409_CONFLICT,
                )

            try:
                try:
                    voucher = Voucher.objects.select_related('batch', 'batch__hotspot_plan').get(code__iexact=voucher_code)
                except Voucher.DoesNotExist:
                    return Response({'error': 'Invalid voucher code'}, status=status.HTTP_404_NOT_FOUND)

                if not voucher.is_valid():
                    if voucher.status == 'EXPIRED' or (voucher.valid_to and voucher.valid_to < timezone.now()):
                        reason = 'Voucher has expired'
                    elif voucher.status in ('USED', 'REDEEMED') or voucher.use_count >= (voucher.max_uses or 1):
                        reason = 'Voucher has already been used'
                    else:
                        reason = 'Voucher is not available'
                    return Response({'error': reason}, status=status.HTTP_400_BAD_REQUEST)

                plan = None
                if hasattr(voucher, 'hotspot_plan') and voucher.hotspot_plan:
                    plan = voucher.hotspot_plan
                elif voucher.batch and voucher.batch.hotspot_plan:
                    plan = voucher.batch.hotspot_plan

                if not plan:
                    return Response(
                        {'error': 'This voucher is not linked to a valid plan'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                if not plan.is_active:
                    return Response(
                        {'error': 'The plan linked to this voucher is no longer available'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

                try:
                    router = Router.objects.get(id=router_id, is_active=True)
                except (Router.DoesNotExist, ValueError):
                    try:
                        router = Router.objects.get(name=router_id, is_active=True)
                    except Router.DoesNotExist:
                        return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)

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

                if voucher.remaining_value is not None and voucher.remaining_value < plan.price:
                    return Response({
                        'error': f'Voucher balance (KES {voucher.remaining_value}) is insufficient for this plan (KES {plan.price})'
                    }, status=status.HTTP_400_BAD_REQUEST)

                from apps.billing.models.hotspot_models import HotspotClient, HotspotClientDevice

                provided_phone = request.data.get('phone_number') or ''
                phone_to_use = provided_phone if provided_phone else f"MAC-{mac_address.replace(':', '')[:11]}"

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

                if hotspot_client and hotspot_client.canonical_username:
                    friendly_username = hotspot_client.canonical_username
                    logger.info(f"🔄 VOUCHER: Using permanent username {friendly_username} for {mac_address}")
                else:
                    friendly_username = self._generate_code()
                    logger.warning(f"⚠️ VOUCHER: No canonical_username, using generated code {friendly_username}")

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
                    hotspot_client=hotspot_client,
                )

                try:
                    session.activate(friendly_username)
                    _close_prior_radacct_rows_for_renewal(session, friendly_username)

                    from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                    ok = HotspotRadiusService().create_hotspot_credentials(
                        username=friendly_username,
                        password=friendly_username,
                        router=session.router,
                        plan=session.plan,
                        expires_at=session.expires_at,
                        mac_address=mac_address,
                    )
                    if not ok:
                        raise RuntimeError("RADIUS provisioning returned False")

                    if is_tv_target:
                        cache.set(f"hs_tv_push:{tenant.schema_name}:{session.session_id}", 1, timeout=3600)
                        schedule_tv_login_push(session, tenant.schema_name)

                    voucher.use_count = (voucher.use_count or 0) + 1
                    if voucher.remaining_value is not None:
                        voucher.remaining_value = max(Decimal('0'), voucher.remaining_value - plan.price)
                    if not voucher.is_reusable or voucher.use_count >= (voucher.max_uses or 1):
                        voucher.status = 'USED'
                    voucher.save()

                    logger.info(f"VOUCHER REDEEM: {voucher.code} -> user {friendly_username} at {router.name}")

                except Exception as e:
                    logger.error(f"Voucher redeem activation failed, rolling back: {e}", exc_info=True)
                    raise

                from apps.messaging.tasks import send_hotspot_welcome_sms
                send_hotspot_welcome_sms.delay(session.session_id, tenant.schema_name)

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

            except Exception as e:
                logger.error(f"Voucher redeem failed for {voucher_code}: {e}", exc_info=True)
                return Response(
                    {'error': 'Could not activate your plan. Please try redeeming again.', 'retriable': True},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            finally:
                cache.delete(lock_key)


# ============================================================
# NEW: Phone number based reconnect / multi‑device management
# ============================================================

class HotspotPhoneReconnectView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    _RATE_LIMIT_TTL = 600
    _RATE_LIMIT_MAX = 5

    def _check_rate_limit(self, tenant_schema: str, phone: str) -> bool:
        cache_key = f"phone_reconnect_attempts:{tenant_schema}:{phone}"
        attempts = cache.get(cache_key, 0)
        if attempts >= self._RATE_LIMIT_MAX:
            return False
        cache.set(cache_key, attempts + 1, timeout=self._RATE_LIMIT_TTL)
        return True

    def _canonicalize_phone(self, phone: str, country_code: str = 'KE') -> str:
        from utils.phone import normalize_phone_number
        return normalize_phone_number(phone, country_code)

    @transaction.atomic
    def post(self, request):
        tenant_subdomain = request.data.get('tenant') or request.query_params.get('tenant')
        raw_phone = (request.data.get('phone_number') or '').strip()
        router_id = request.data.get('router_id')
        mac_address = _normalize_mac(request.data.get('mac_address', ''))

        if not tenant_subdomain:
            return Response({'error': 'Tenant is required'}, status=status.HTTP_400_BAD_REQUEST)
        if not raw_phone:
            return Response({'error': 'Phone number is required'}, status=status.HTTP_400_BAD_REQUEST)
        if not router_id:
            return Response({'error': 'Router ID is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from apps.core.models import Tenant
            with schema_context(get_public_schema_name()):
                tenant = Tenant.objects.get(
                    Q(subdomain=tenant_subdomain) | Q(schema_name=tenant_subdomain),
                    is_active=True
                )
        except Exception:
            return Response({'error': 'Invalid tenant'}, status=status.HTTP_400_BAD_REQUEST)

        customer_country = _get_tenant_country(tenant)

        phone_canonical = self._canonicalize_phone(raw_phone, customer_country)

        if not phone_canonical:
            return Response(
                {'error': 'Invalid phone number format. Please enter a valid number.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not self._check_rate_limit(tenant.schema_name, phone_canonical):
            return Response(
                {
                    'error': 'Too many attempts. Please wait 10 minutes before trying again.',
                    'rate_limited': True,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        with schema_context(tenant.schema_name):
            now = timezone.now()

            try:
                router = Router.objects.get(id=router_id, is_active=True)
            except (Router.DoesNotExist, ValueError):
                try:
                    router = Router.objects.get(name=router_id, is_active=True)
                except Router.DoesNotExist:
                    return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)

            phone_local = '0' + phone_canonical[3:] if len(phone_canonical) > 3 else ''
            phone_short = phone_canonical[3:] if len(phone_canonical) > 3 else ''

            client = HotspotClient.objects.filter(
                Q(canonical_phone=phone_canonical) |
                Q(canonical_phone=phone_local) |
                Q(canonical_phone=phone_short)
            ).first()

            if not client:
                direct_session = HotspotSession.objects.filter(
                    Q(phone_number=phone_canonical) |
                    Q(phone_number=phone_local) |
                    Q(phone_number=phone_short),
                    status='active',
                    expires_at__gt=now,
                ).select_related('hotspot_client', 'plan', 'router').order_by('-created_at').first()

                if direct_session:
                    client = direct_session.hotspot_client

                    if not client:
                        plan = direct_session.plan
                        plan_device_limit = getattr(plan, 'simultaneous_devices', 1) or 1

                        if plan_device_limit <= 1:
                            base_session = direct_session
                            if mac_address and mac_address != '00:00:00:00:00:00':
                                base_session.mac_address = mac_address
                                base_session.save(update_fields=['mac_address'])

                            access_code = base_session.access_code
                            try:
                                from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                                HotspotRadiusService().create_hotspot_credentials(
                                    username=access_code,
                                    password=access_code,
                                    router=router,
                                    plan=plan,
                                    expires_at=base_session.expires_at,
                                    mac_address=mac_address,
                                )
                            except Exception as e:
                                logger.error(f"Phone reconnect single-device RADIUS reseed failed: {e}")
                                return Response(
                                    {'error': 'Failed to restore connection. Please try again.'},
                                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                                )

                            # NEW: free the router-side slot (ghost / old MAC) once the
                            # surrounding DB transaction commits, before the portal
                            # submits the login to MikroTik.
                            _kick_router_slot_after_commit(access_code, router)

                            remaining_minutes = max(
                                0, int((base_session.expires_at - now).total_seconds() / 60)
                            )
                            logger.info(
                                f"Phone reconnect (MAC rotation, same owner): phone={phone_canonical} "
                                f"old_mac={base_session.mac_address} new_mac={mac_address} "
                                f"access_code={access_code}"
                            )
                            return Response({
                                'status': 'reconnected',
                                'message': 'Welcome back! Your connection has been restored.',
                                'access_code': access_code,
                                'expires_at': base_session.expires_at.isoformat(),
                                'remaining_minutes': remaining_minutes,
                                'plan_name': plan.name,
                                'device_slot': 'existing',
                                'credentials': {'username': access_code, 'password': access_code},
                            })

                        is_same_device = (
                            mac_address and 
                            direct_session.mac_address and 
                            mac_address.upper() == direct_session.mac_address.upper()
                        )

                        if plan_device_limit <= 1 and not is_same_device:
                            return Response(
                                {
                                    'error': 'Your plan supports only 1 device. '
                                             'To connect a different device, disconnect the current one first.',
                                    'single_device_plan': True,
                                },
                                status=status.HTTP_403_FORBIDDEN
                            )

                        access_code = direct_session.access_code
                        try:
                            from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                            HotspotRadiusService().create_hotspot_credentials(
                                username=access_code,
                                password=access_code,
                                router=router,
                                plan=plan,
                                expires_at=direct_session.expires_at,
                                mac_address=mac_address,
                            )
                        except Exception as e:
                            logger.error(f"Phone reconnect (direct session path) RADIUS failed: {e}")
                            return Response(
                                {'error': 'Failed to restore connection. Please try again.'},
                                status=status.HTTP_500_INTERNAL_SERVER_ERROR
                            )

                        # NEW: free the router-side slot (ghost / old MAC) once the
                        # surrounding DB transaction commits.
                        _kick_router_slot_after_commit(access_code, router)

                        remaining_minutes = max(
                            0, int((direct_session.expires_at - now).total_seconds() / 60)
                        )
                        return Response({
                            'status': 'reconnected',
                            'message': 'Connection restored!',
                            'access_code': access_code,
                            'expires_at': direct_session.expires_at.isoformat(),
                            'remaining_minutes': remaining_minutes,
                            'plan_name': plan.name,
                            'device_slot': 'existing',
                            'credentials': {'username': access_code, 'password': access_code},
                        })

            if not client:
                return Response(
                    {
                        'error': 'No subscription found for this number. '
                                 'Please purchase a plan first.',
                        'no_subscription': True,
                    },
                    status=status.HTTP_404_NOT_FOUND
                )

            slot_lock_key = f"phone_reconnect_slot_lock:{tenant.schema_name}:{client.id}"
            if not cache.add(slot_lock_key, "1", timeout=10):
                return Response(
                    {'error': 'Another device is connecting right now. Please try again in a few seconds.'},
                    status=status.HTTP_409_CONFLICT,
                )

            try:
                logger.info(
                    f"Phone reconnect lookup: raw={raw_phone} canonical={phone_canonical} "
                    f"client_id={client.id} client_phone={client.canonical_phone} "
                    f"client_username={client.canonical_username}"
                )

                active_sessions = HotspotSession.objects.filter(
                    Q(hotspot_client=client) |
                    Q(phone_number=phone_canonical) |
                    Q(phone_number=phone_local) |
                    Q(phone_number=phone_short),
                    status='active',
                    expires_at__gt=now,
                ).select_related('plan', 'router').order_by('-activated_at').distinct()

                logger.info(
                    f"Active sessions found: {active_sessions.count()} "
                    f"for client {client.canonical_username}"
                )

                if not active_sessions.exists():
                    paid_session = HotspotSession.objects.filter(
                        Q(hotspot_client=client) |
                        Q(phone_number=phone_canonical) |
                        Q(phone_number=phone_local) |
                        Q(phone_number=phone_short),
                        status='paid',
                    ).order_by('-created_at').first()

                    if paid_session:
                        paid_session.activate(paid_session.access_code or HotspotSession.generate_access_code())

                        try:
                            from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                            HotspotRadiusService().create_hotspot_credentials(
                                username=paid_session.access_code,
                                password=paid_session.access_code,
                                router=paid_session.router,
                                plan=paid_session.plan,
                                expires_at=paid_session.expires_at,
                                mac_address=mac_address,
                            )
                        except Exception as e:
                            logger.error(f"Phone reconnect paid->active RADIUS failed: {e}")

                        active_sessions = HotspotSession.objects.filter(
                            Q(hotspot_client=client) |
                            Q(phone_number=phone_canonical) |
                            Q(phone_number=phone_local) |
                            Q(phone_number=phone_short),
                            status='active',
                            expires_at__gt=now,
                        ).select_related('plan', 'router').order_by('-activated_at').distinct()

                if not active_sessions.exists():
                    return Response(
                        {
                            'error': 'No active subscription found for this number. '
                                     'Your plan may have expired.',
                            'expired': True,
                        },
                        status=status.HTTP_404_NOT_FOUND
                    )

                base_session = active_sessions.filter(
                    access_code=client.canonical_username
                ).first() or active_sessions.first()

                plan = base_session.plan
                plan_device_limit = getattr(plan, 'simultaneous_devices', 1) or 1

                existing_mac_session = active_sessions.filter(mac_address=mac_address).first()

                if plan_device_limit <= 1 and not existing_mac_session:
                    base_session_updated = base_session
                    if mac_address and mac_address != '00:00:00:00:00:00':
                        base_session_updated.mac_address = mac_address
                        base_session_updated.save(update_fields=['mac_address'])
                        if base_session_updated.hotspot_client:
                            HotspotClientDevice.record_device(
                                client=base_session_updated.hotspot_client,
                                mac_address=mac_address
                            )

                    access_code = base_session.access_code
                    try:
                        from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                        HotspotRadiusService().create_hotspot_credentials(
                            username=access_code,
                            password=access_code,
                            router=router,
                            plan=plan,
                            expires_at=base_session.expires_at,
                            mac_address=mac_address,
                        )
                    except Exception as e:
                        logger.error(f"Phone reconnect single-device RADIUS reseed failed: {e}")
                        return Response(
                            {'error': 'Failed to restore connection. Please try again.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR
                        )

                    # NEW: free the router-side slot (ghost / old MAC) once the
                    # surrounding DB transaction commits.
                    _kick_router_slot_after_commit(access_code, router)

                    remaining_minutes = max(
                        0, int((base_session.expires_at - now).total_seconds() / 60)
                    )
                    logger.info(
                        f"Phone reconnect (MAC rotation, same owner): phone={phone_canonical} "
                        f"old_mac={base_session.mac_address} new_mac={mac_address} "
                        f"access_code={access_code}"
                    )
                    return Response({
                        'status': 'reconnected',
                        'message': 'Welcome back! Your connection has been restored.',
                        'access_code': access_code,
                        'expires_at': base_session.expires_at.isoformat(),
                        'remaining_minutes': remaining_minutes,
                        'plan_name': plan.name,
                        'device_slot': 'existing',
                        'credentials': {'username': access_code, 'password': access_code},
                    })

                existing_session_for_mac = active_sessions.filter(
                    mac_address=mac_address
                ).first()

                if existing_session_for_mac:
                    access_code = existing_session_for_mac.access_code
                    try:
                        from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                        HotspotRadiusService().create_hotspot_credentials(
                            username=access_code,
                            password=access_code,
                            router=router,
                            plan=plan,
                            expires_at=existing_session_for_mac.expires_at,
                            mac_address=mac_address,
                        )
                    except Exception as e:
                        logger.error(f"Phone reconnect RADIUS reseed failed: {e}")
                        return Response(
                            {'error': 'Failed to restore connection. Please try again.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR
                        )

                    # NEW: free the router-side slot (ghost / old MAC) once the
                    # surrounding DB transaction commits. This branch was missed
                    # in the original fix — a ghost entry at a new IP hits the
                    # same "no more sessions are allowed" error here.
                    _kick_router_slot_after_commit(access_code, router)

                    remaining_minutes = max(
                        0, int((existing_session_for_mac.expires_at - now).total_seconds() / 60)
                    )
                    logger.info(
                        f"Phone reconnect (existing MAC): phone={phone_canonical} "
                        f"mac={mac_address} access_code={access_code}"
                    )
                    return Response({
                        'status': 'reconnected',
                        'message': 'Welcome back! Your connection has been restored.',
                        'access_code': access_code,
                        'expires_at': existing_session_for_mac.expires_at.isoformat(),
                        'remaining_minutes': remaining_minutes,
                        'plan_name': plan.name,
                        'device_slot': 'existing',
                        'credentials': {'username': access_code, 'password': access_code},
                    })

                occupied_slots = active_sessions.values('access_code').distinct().count()

                if occupied_slots >= plan_device_limit:
                    stale_sessions = active_sessions.exclude(mac_address=mac_address).order_by('activated_at')

                    if stale_sessions.exists():
                        oldest_stale = stale_sessions.first()

                        logger.info(
                            f"MAC rotation takeover: phone={phone_canonical} "
                            f"replacing stale session {oldest_stale.access_code} "
                            f"old_mac={oldest_stale.mac_address} new_mac={mac_address}"
                        )

                        oldest_stale.mac_address = mac_address
                        oldest_stale.save(update_fields=['mac_address'])

                        HotspotClientDevice.record_device(client=client, mac_address=mac_address)

                        access_code = oldest_stale.access_code
                        try:
                            from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                            HotspotRadiusService().create_hotspot_credentials(
                                username=access_code,
                                password=access_code,
                                router=router,
                                plan=plan,
                                expires_at=oldest_stale.expires_at,
                                mac_address=mac_address,
                            )
                        except Exception as e:
                            logger.error(f"Phone reconnect MAC-rotation takeover RADIUS failed: {e}")
                            return Response(
                                {'error': 'Failed to restore connection. Please try again.'},
                                status=status.HTTP_500_INTERNAL_SERVER_ERROR
                            )

                        # NEW: free the router-side slot (ghost / old MAC) once the
                        # surrounding DB transaction commits. Without this the
                        # router still thinks the old device is active and refuses
                        # the new login with "no more sessions are allowed".
                        _kick_router_slot_after_commit(access_code, router)

                        remaining_minutes = max(
                            0, int((oldest_stale.expires_at - now).total_seconds() / 60)
                        )
                        logger.info(
                            f"Phone reconnect (MAC rotation slot takeover): phone={phone_canonical} "
                            f"old_mac={oldest_stale.mac_address} new_mac={mac_address} "
                            f"access_code={access_code}"
                        )
                        return Response({
                            'status': 'reconnected',
                            'message': 'Welcome back! Your connection has been restored.',
                            'access_code': access_code,
                            'expires_at': oldest_stale.expires_at.isoformat(),
                            'remaining_minutes': remaining_minutes,
                            'plan_name': plan.name,
                            'device_slot': 'existing',
                            'credentials': {'username': access_code, 'password': access_code},
                        })

                    return Response(
                        {
                            'error': (
                                f'Your plan supports {plan_device_limit} device'
                                f'{"s" if plan_device_limit > 1 else ""}. '
                                f'All slots are in use. '
                                f'Disconnect one of your other devices to connect this one.'
                            ),
                            'slots_full': True,
                            'device_limit': plan_device_limit,
                            'occupied_slots': occupied_slots,
                        },
                        status=status.HTTP_403_FORBIDDEN
                    )

                device_slot = occupied_slots + 1

                if device_slot == 1 and client.canonical_username:
                    new_access_code = client.canonical_username
                else:
                    base_username = client.canonical_username or base_session.access_code
                    new_access_code = f"{base_username}-{device_slot}"
                    attempt = device_slot
                    while HotspotSession.objects.filter(
                        access_code=new_access_code,
                        status='active',
                        expires_at__gt=now,
                    ).exists():
                        attempt += 1
                        new_access_code = f"{base_username}-{attempt}"

                HotspotClientDevice.record_device(client=client, mac_address=mac_address)

                new_session = HotspotSession.objects.create(
                    session_id=HotspotSession.generate_session_id(),
                    router=router,
                    plan=plan,
                    phone_number=phone_canonical,
                    mac_address=mac_address,
                    amount=Decimal('0'),
                    status='active',
                    access_code=new_access_code,
                    radius_username=new_access_code,
                    activated_at=now,
                    expires_at=base_session.expires_at,
                    hotspot_client=client,
                )

                # NOTE: No kick_active_session here — a brand-new slot has
                # nothing to kick. Only genuine takeovers (above) kick.
                try:
                    from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                    HotspotRadiusService().create_hotspot_credentials(
                        username=new_access_code,
                        password=new_access_code,
                        router=router,
                        plan=plan,
                        expires_at=base_session.expires_at,
                        mac_address=mac_address,
                    )
                except Exception as e:
                    new_session.delete()
                    logger.error(f"Phone reconnect new-device RADIUS failed: {e}")
                    return Response(
                        {'error': 'Failed to connect new device. Please try again.'},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )

                remaining_minutes = max(
                    0, int((base_session.expires_at - now).total_seconds() / 60)
                )

                logger.info(
                    f"Phone reconnect (new device slot {device_slot}): "
                    f"phone={phone_canonical} mac={mac_address} "
                    f"access_code={new_access_code} plan_limit={plan_device_limit}"
                )

                return Response({
                    'status': 'new_device_connected',
                    'message': f'Device {device_slot} of {plan_device_limit} connected successfully!',
                    'access_code': new_access_code,
                    'expires_at': base_session.expires_at.isoformat(),
                    'remaining_minutes': remaining_minutes,
                    'plan_name': plan.name,
                    'device_slot': device_slot,
                    'device_limit': plan_device_limit,
                    'credentials': {'username': new_access_code, 'password': new_access_code},
                })

            finally:
                cache.delete(slot_lock_key)


# ============================================================
# NEW: Credential (username + password) based reconnect
# ============================================================

class _ReconnectError(Exception):
    def __init__(self, message, http_status):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


class HotspotCredentialReconnectView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    _TTL = 600
    _IP_LIMIT = 20
    _USER_LIMIT = 5
    _INVALID = 'Invalid username or password, or your plan has expired.'

    @staticmethod
    def _client_ip(request):
        xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
        ip = xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR', '')
        return ip or 'unknown'

    def _hit(self, key):
        if cache.add(key, 1, timeout=self._TTL):
            return 1
        try:
            return cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=self._TTL)
            return 1

    @staticmethod
    def _active_session(code, now):
        return (
            HotspotSession.objects
            .filter(access_code=code, status='active', expires_at__gt=now)
            .select_related('plan', 'router', 'hotspot_client')
            .order_by('-activated_at')
            .first()
        )

    def _provision_extra_slot(self, schema, username, mac_address, now):
        m = SLOT_CODE_RE.match(username)
        if not m:
            return None
        slot = int(m.group('slot'))
        base = self._active_session(m.group('base'), now)
        if not base or slot < 2 or slot > device_limit(base.plan):
            return None

        if not cache.add(f"cred_slot_lock:{schema}:{username}", '1', timeout=10):
            raise _ReconnectError('Another connection is in progress. Please retry shortly.', 409)

        existing = self._active_session(username, now)
        if existing:
            return existing

        return HotspotSession.objects.create(
            session_id=HotspotSession.generate_session_id(),
            router=base.router,
            plan=base.plan,
            phone_number=base.phone_number,
            mac_address=mac_address,
            amount=Decimal('0'),
            status='active',
            access_code=username,
            radius_username=username,
            activated_at=now,
            expires_at=base.expires_at,
            hotspot_client=base.hotspot_client,
        )

    def post(self, request):
        data = request.data
        tenant_subdomain = data.get('tenant') or request.query_params.get('tenant')
        router_id = data.get('router_id')
        username = (data.get('username') or '').strip().upper()
        password = (data.get('password') or '').strip()
        mac_address = _normalize_mac(data.get('mac_address', ''))

        if not (tenant_subdomain and router_id and username and password):
            return Response({'error': 'Username and password are required'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(username) > 25 or len(password) > 64:
            return Response({'error': self._INVALID}, status=status.HTTP_401_UNAUTHORIZED)

        tenant_info = resolve_tenant_cached(tenant_subdomain)
        if not tenant_info:
            return Response({'error': 'Invalid tenant'}, status=status.HTTP_400_BAD_REQUEST)
        schema = tenant_info['schema_name']

        ip_key = f"cred_reconnect_ip:{schema}:{self._client_ip(request)}"
        user_key = f"cred_reconnect_user:{schema}:{username}"
        if self._hit(ip_key) > self._IP_LIMIT or self._hit(user_key) > self._USER_LIMIT:
            return Response(
                {'error': 'Too many attempts. Please wait 10 minutes before trying again.',
                 'rate_limited': True},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        with schema_context(schema):
            now = timezone.now()

            try:
                router = Router.objects.get(id=router_id, is_active=True)
            except (Router.DoesNotExist, ValueError):
                router = Router.objects.filter(name=router_id, is_active=True).first()
            if router is None:
                return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)

            from apps.radius.models import RadCheck
            stored = (
                RadCheck.objects
                .filter(username=username, attribute='Cleartext-Password')
                .values_list('value', flat=True)
                .first()
            ) or device_password(username)
            if not hmac.compare_digest(str(stored).encode(), password.encode()):
                return Response({'error': self._INVALID}, status=status.HTTP_401_UNAUTHORIZED)

            session = self._active_session(username, now)
            if session is None and not SLOT_CODE_RE.match(username):
                return Response({'error': self._INVALID}, status=status.HTTP_401_UNAUTHORIZED)

            try:
                with transaction.atomic():
                    if session is None:
                        session = self._provision_extra_slot(schema, username, mac_address, now)
                        if session is None:
                            raise _ReconnectError(self._INVALID, 401)

                    mac_ok = bool(mac_address) and mac_address != '00:00:00:00:00:00'
                    if mac_ok and session.mac_address != mac_address:
                        session.mac_address = mac_address
                        session.save(update_fields=['mac_address', 'updated_at'])
                    if mac_ok and session.hotspot_client:
                        HotspotClientDevice.record_device(
                            client=session.hotspot_client, mac_address=mac_address
                        )

                    from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                    ok = HotspotRadiusService().create_hotspot_credentials(
                        username=session.access_code,
                        password=password,
                        router=router,
                        plan=session.plan,
                        expires_at=session.expires_at,
                        mac_address=mac_address,
                    )
                    if not ok:
                        raise _ReconnectError('Failed to restore connection. Please try again.', 503)
            except _ReconnectError as e:
                return Response({'error': e.message}, status=e.http_status)

            # NEW: free the router-side slot (old device / ghost entry) before
            # the portal submits the login to MikroTik. Runs AFTER the inner
            # with transaction.atomic() block has committed, so no DB
            # transaction is held open during the network call to the router.
            from apps.billing.services.hotspot_radius_service import HotspotRadiusService
            HotspotRadiusService().kick_active_session(session.access_code, router)

            cache.delete(user_key)

            remaining_minutes = max(0, int((session.expires_at - now).total_seconds() / 60))
            logger.info(f"Credential reconnect: user={username} mac={mac_address} schema={schema}")
            return Response({
                'status': 'reconnected',
                'message': 'Welcome back! Your connection has been restored.',
                'access_code': session.access_code,
                'expires_at': session.expires_at.isoformat(),
                'remaining_minutes': remaining_minutes,
                'plan_name': session.plan.name,
                'device_slot': 'existing',
                'credentials': {'username': session.access_code, 'password': password},
            })


# ============================================================
# NEW: Hotspot Free Trial View
# ============================================================

class HotspotFreeTrialView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    @transaction.atomic
    def post(self, request):
        tenant_subdomain = request.data.get('tenant') or request.query_params.get('tenant')
        router_id = request.data.get('router_id')
        plan_id = request.data.get('plan_id')
        mac_address = _normalize_mac(request.data.get('mac_address', ''))

        if not all([tenant_subdomain, router_id, plan_id]):
            return Response({'error': 'tenant, router_id, and plan_id are required'}, status=400)

        if not mac_address or mac_address == '00:00:00:00:00:00':
            return Response({'error': 'Valid MAC address is required'}, status=400)

        try:
            from apps.core.models import Tenant
            with schema_context(get_public_schema_name()):
                tenant = Tenant.objects.get(subdomain=tenant_subdomain, is_active=True)
        except Exception:
            return Response({'error': 'Invalid tenant'}, status=400)

        with schema_context(tenant.schema_name):
            if HotspotFreeTrialUsage.objects.filter(mac_address=mac_address).exists():
                return Response({
                    'error': 'This device has already used the free trial.',
                    'already_claimed': True,
                }, status=400)

            try:
                router = Router.objects.get(id=router_id, is_active=True)
            except (Router.DoesNotExist, ValueError):
                return Response({'error': 'Router not found'}, status=404)

            try:
                plan = HotspotPlan.objects.get(id=plan_id, router=router, is_active=True, is_free_trial=True)
            except HotspotPlan.DoesNotExist:
                return Response({'error': 'Free trial plan not found'}, status=404)

            hotspot_client = HotspotClient.get_or_create_by_mac(
                schema_name=tenant.schema_name,
                mac_address=mac_address,
            )
            if hotspot_client and mac_address:
                HotspotClientDevice.record_device(client=hotspot_client, mac_address=mac_address)

            if hotspot_client and hotspot_client.canonical_username:
                access_code = hotspot_client.canonical_username
            else:
                access_code = HotspotSession.generate_access_code()

            session_id = HotspotSession.generate_session_id()
            session = HotspotSession.objects.create(
                session_id=session_id,
                router=router,
                plan=plan,
                phone_number='FREE_TRIAL',
                mac_address=mac_address,
                amount=0,
                status='paid',
                access_code=access_code,
                hotspot_client=hotspot_client,
            )
            session.activate(access_code)

            HotspotFreeTrialUsage.objects.create(
                mac_address=mac_address,
                router=router,
                access_code=access_code,
            )

            try:
                from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                HotspotRadiusService().create_hotspot_credentials(
                    username=access_code,
                    password=access_code,
                    router=router,
                    plan=plan,
                    expires_at=session.expires_at,
                    mac_address=mac_address,
                )
            except Exception as e:
                logger.error(f"Free trial RADIUS provisioning failed: {e}")

            from apps.messaging.tasks import send_hotspot_welcome_sms
            send_hotspot_welcome_sms.delay(session.session_id, tenant.schema_name)

            return Response({
                'status': 'success',
                'access_code': access_code,
                'expires_at': session.expires_at,
                'duration_display': plan.duration_display,
                'plan_name': plan.name,
                'message': f'Enjoy your free {plan.duration_display} of internet!',
            }, status=201)