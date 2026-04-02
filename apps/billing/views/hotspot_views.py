"""
Hotspot Views for Captive Portal Payments

These are PUBLIC endpoints - no authentication required.
End users access these when connecting to WiFi hotspots.
"""

import logging
import random
import string
import time
from decimal import Decimal

from django.conf import settings
from django.db import transaction, ProgrammingError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone

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
from apps.network.models.router_models import Router
from apps.subscriptions.models import CommissionLedger

logger = logging.getLogger(__name__)


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
        # Validity
        'validity_type': plan.validity_type or 'DAYS',
        'validity_value': _plan_validity_value(plan),
        'duration_display': plan.validity_display,
        # Speed
        'download_speed': plan.download_speed or 0,
        'upload_speed': plan.upload_speed or 0,
        'speed_unit': plan.speed_unit or 'MBPS',
        'speed_display': plan.speed_display,
        # Data limits
        'limitation_type': 'UNLIMITED' if plan.data_limit is None else 'DATA',
        'data_limit_value': plan.data_limit,
        'data_limit_unit': 'GB',
        'data_limit_display': _plan_data_limit_display(plan),
        # Display flags
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
        # Validity
        'validity_type': plan.validity_type,
        'validity_value': plan.validity_value,
        'duration_display': plan.duration_display,
        # Speed
        'download_speed': plan.download_speed,
        'upload_speed': plan.upload_speed,
        'speed_unit': plan.speed_unit,
        'speed_display': plan.speed_display,
        # Data limits
        'limitation_type': plan.limitation_type,
        'data_limit_value': plan.data_limit_value,
        'data_limit_unit': plan.data_limit_unit,
        'data_limit_display': plan.data_limit_display,
        # Device limits
        'simultaneous_devices': plan.simultaneous_devices,
        # Display flags
        'is_popular': plan.is_popular,
    }


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

        # ── Resolve the tenant from the public schema ──
        try:
            from apps.core.models import Tenant
            with schema_context(get_public_schema_name()):
                tenant = Tenant.objects.get(Q(subdomain=tenant_subdomain) | Q(schema_name=tenant_subdomain), is_active=True)
        except Exception as e:
            logger.error(f"Tenant '{tenant_subdomain}' not found: {e}")
            return Response({'status': 'error', 'message': 'Tenant not found'}, status=status.HTTP_400_BAD_REQUEST)

        # ── Query router + plans inside the tenant schema ──
        try:
            with schema_context(tenant.schema_name):
                # ── Find the Router ──
                router = None
                try:
                    router = Router.objects.get(id=router_id, is_active=True)
                except (Router.DoesNotExist, ValueError):
                    try:
                        router = Router.objects.get(name=router_id, is_active=True)
                        logger.info("CaptivePortal: router found by name '%s' -> id=%s", router_id, router.id)
                    except Router.DoesNotExist:
                        logger.warning(f"Router '{router_id}' does not exist in tenant {tenant_subdomain}")
                except ProgrammingError:
                    # Table doesn't exist yet — tenant schema not fully migrated
                    logger.warning("CaptivePortal: network_router table missing for tenant %s", tenant_subdomain)

                if router is None:
                    # Even without a router, we can still serve tenant-wide
                    # Plan records so the portal shows *something*.
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

                    # ── Load branding for this router ──
                    branding_data = None
                    try:
                        branding = getattr(router, 'hotspot_branding', None)
                        if branding is None:
                            branding = HotspotBranding.objects.filter(is_default=True).first()
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
                            # Merge branding support_phone into portal_config if not set on router
                            if not portal_config['support_phone'] and branding.support_phone:
                                portal_config['support_phone'] = branding.support_phone
                    except Exception:
                        logger.debug("CaptivePortal: no branding found for router %s", router_id)

                # ── Resolve Plans ──
                # Priority 1: HotspotPlan records for this router
                plans_data = []
                if router is not None:
                    try:
                        hotspot_plans = HotspotPlan.objects.filter(
                            router=router,
                            is_active=True,
                        ).order_by('sort_order', 'price')
                        plans_data = [_serialize_hotspot_plan(p) for p in hotspot_plans]
                    except ProgrammingError:
                        logger.warning("CaptivePortal: billing_hotspotplan table missing for tenant %s", tenant_subdomain)

                # Priority 2: Fallback to Plan(plan_type='HOTSPOT') — tenant-wide
                if not plans_data:
                    try:
                        generic_plans = Plan.objects.filter(
                            plan_type='HOTSPOT',
                            is_active=True,
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
        
        return Response({
            'status': 'success',
            'portal_config': portal_config,
            'branding': branding_data,
            'plans': plans_data,
        })


class HotspotPlansView(APIView):
    """
    Get hotspot plans for a specific router.
    
    PUBLIC ENDPOINT - No authentication required.
    
    GET /api/v1/hotspot/routers/{router_id}/plans/
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []  # No auth required
    
    def get(self, request, router_id):
        # BULLETPROOF: Try by ID, fallback to Name
        try:
            router = Router.objects.get(id=router_id, is_active=True)
        except (Router.DoesNotExist, ValueError):
            try:
                router = Router.objects.get(name=router_id, is_active=True)
                logger.info(f"Router found by name in HotspotPlansView: {router_id} -> {router.id}")
            except Router.DoesNotExist:
                return Response(
                    {'error': 'Router not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
        
        # Get active plans for this router
        plans = HotspotPlan.objects.filter(
            router=router,
            is_active=True
        ).order_by('sort_order', 'price')
        
        # Get branding
        try:
            branding = router.hotspot_branding
        except HotspotBranding.DoesNotExist:
            # Try to get default branding
            branding = HotspotBranding.objects.filter(
                is_default=True
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
                'welcome_title': branding.welcome_title,
                'welcome_message': branding.welcome_message,
                'support_phone': branding.support_phone,
                'support_email': branding.support_email,
            }
        
        # Portal customisation stored on the Router model
        portal_config = {
            'template_id': router.template_id or 1,
            'hotspot_name': router.hotspot_name or router.name,
            'support_phone': router.support_phone or (branding.support_phone if branding else ''),
            'announcement_text': router.announcement_text or '',
        }

        return Response({
            'router': {
                'id': router.id,
                'name': router.name,
                'location': router.location,
            },
            'plans': plans_data,
            'branding': branding_data,
            'portal_config': portal_config,
        })


class HotspotPurchaseView(APIView):
    """
    Initiate hotspot purchase with REAL STK Push via Tuma.
    Creates pending session, initiates STK payment, and returns pending status.
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []  # No auth required
    
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
            
            # Ensure strictly unique in this tenant's database
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

        # Run inside the tenant's schema
        with schema_context(tenant.schema_name):
            router_id = request.data.get('router_id')
            plan_id = request.data.get('plan_id')
            phone_number = request.data.get('phone_number')
            mac_address = request.data.get('mac_address', '')
            
            # Validate required fields
            if not all([router_id, plan_id, phone_number]):
                return Response({
                    'error': 'Missing required fields: router_id, plan_id, phone_number'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            # 1. Find Router & Plan
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
            
            # 2. Normalize MAC (Format: AA:BB:CC:DD:EE:FF)
            mac_address = mac_address.upper().replace('-', ':')
            
            # 3. PERSISTENT IDENTITY & ROAMING DETECTOR
            existing_user = HotspotSession.objects.filter(
                mac_address=mac_address
            ).exclude(
                access_code__isnull=True
            ).order_by('-created_at').first()

            is_roaming = False
            roamed_from_name = None

            if existing_user and existing_user.access_code:
                # RETURNING CUSTOMER
                friendly_username = existing_user.access_code
                
                # ROAMING CHECK
                if existing_user.router_id != router.id:
                    is_roaming = True
                    roamed_from_name = existing_user.router.name
                    logger.info(f"📍 ROAMING DETECTED: User {friendly_username} moved from {roamed_from_name} to {router.name}")
                else:
                    logger.info(f"🏠 HOME ROUTER: User {friendly_username} returning to {router.name}")
                    
            else:
                # BRAND NEW CUSTOMER
                friendly_username = self.generate_unique_code()
                logger.info(f"✨ NEW USER: {mac_address} -> {friendly_username} at {router.name}")

            # ═══════════════════════════════════════════════════════════
            # 4. CREATE PENDING HOTSPOT SESSION (before payment)
            # ═══════════════════════════════════════════════════════════
            session_id = HotspotSession.generate_session_id()
            session = HotspotSession.objects.create(
                session_id=session_id,
                router=router,
                plan=plan,
                phone_number=phone_number,
                mac_address=mac_address,
                amount=plan.price,
                status='pending',  # NOT paid yet - waiting for STK
                access_code=friendly_username,
                is_roaming=is_roaming,
                roamed_from=roamed_from_name
            )

            # ═══════════════════════════════════════════════════════════
            # 5. RESOLVE TUMA CONFIGURATION
            # ═══════════════════════════════════════════════════════════
            cfg = TenantTumaConfig.objects.filter(schema_name=tenant.schema_name, is_active=True).first()
            if not cfg or not cfg.tuma_business_email or not cfg.tuma_business_api_key:
                session.mark_failed("Tuma gateway not configured")
                return Response({'error': 'Payment gateway not configured'}, status=status.HTTP_400_BAD_REQUEST)

            # ═══════════════════════════════════════════════════════════
            # 6. GET OR CREATE PAYMENT METHOD FOR HOTSPOT STK
            # ═══════════════════════════════════════════════════════════
            payment_method, _ = InvoiceItemPayment.objects.get_or_create(
                schema_name=tenant.schema_name,
                code='HOTSPOT_TUMA_STK',
                defaults={
                    'name': 'Hotspot M-Pesa STK (Tuma)',
                    'method_type': 'MPESA_STK',
                    'is_active': True,
                    'minimum_amount': 1,
                    'maximum_amount': 1000000,
                    'tuma_configuration': cfg,
                }
            )

            # ═══════════════════════════════════════════════════════════
            # 7. CREATE PAYMENT ROW LINKED TO HOTSPOT SESSION
            # ═══════════════════════════════════════════════════════════
            payment_ref = f"HS-{session.session_id}-{int(time.time())}".replace(" ", "-")
            
            # Create or get a system "Hotspot Walk-in" customer
            from apps.customers.models import Customer
            hotspot_customer, _ = Customer.objects.get_or_create(
                schema_name=tenant.schema_name,
                email__iexact='hotspot@system.local',
                defaults={
                    'customer_code': 'HOTSPOT_WALKIN',
                    'full_name': 'Hotspot Walk-in Customer',
                    'phone': phone_number,  # Use the provided phone number
                    'email': 'hotspot@system.local',
                    'is_active': True,
                }
            )
            
            payment = Payment.objects.create(
                customer=hotspot_customer,  # Required field
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

            # ═══════════════════════════════════════════════════════════
            # 8. INITIATE STK PUSH VIA TUMA
            # ═══════════════════════════════════════════════════════════
            try:
                client = TumaClient()
                token = client.get_token(cfg.tuma_business_email, cfg.tuma_business_api_key)
                description = f"HS-{session.session_id}"
                
                # Get callback URL from settings or use default
                callback_url = getattr(settings, 'TUMA_CALLBACK_URL', None)
                if not callback_url:
                    # Construct default callback URL based on tenant
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
                
                # Update payment with Tuma request IDs
                d = tuma_res.get("data", {})
                payment.tuma_merchant_request_id = d.get("merchant_request_id", "")
                payment.tuma_checkout_request_id = d.get("checkout_request_id", "")
                payment.save(update_fields=['tuma_merchant_request_id', 'tuma_checkout_request_id'])
                
                # Link payment to session
                session.tuma_merchant_request_id = payment.tuma_merchant_request_id
                session.tuma_checkout_request_id = payment.tuma_checkout_request_id
                session.payment = payment
                session.save(update_fields=['tuma_merchant_request_id', 'tuma_checkout_request_id', 'payment'])
                
                logger.info(f"STK Push initiated for session {session.session_id}, payment {payment.payment_number}")
                
            except Exception as e:
                logger.error(f"STK initiation failed for session {session.session_id}: {e}")
                payment.status = 'FAILED'
                payment.tuma_status = 'failed'
                payment.failure_reason = str(e)
                payment.save(update_fields=['status', 'tuma_status', 'failure_reason'])
                session.mark_failed(payment.failure_reason)
                return Response({'error': 'Failed to initiate payment'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # ═══════════════════════════════════════════════════════════
            # 9. RETURN PENDING RESPONSE - WAITING FOR STK COMPLETION
            # ═══════════════════════════════════════════════════════════
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
    authentication_classes = []  # No auth required
    
    def _check_tuma_payment_status(self, session, tenant_schema):
        """
        Check Tuma payment status for a hotspot session.
        
        Priority order:
            1. Explicit payment FK (most reliable)
            2. Tuma request IDs (if stored in session)
            3. Phone number + amount (legacy fallback)
        
        Returns:
            tuple: (status, message, data) where status is one of:
                - 'completed': Payment successful
                - 'failed': Payment failed
                - 'pending': Still pending
        """
        payment = None
        
        # 0) STRONGEST MATCH: Explicit relation via payment FK
        if getattr(session, 'payment_id', None):
            try:
                payment = Payment.objects.filter(id=session.payment_id).first()
                if payment:
                    logger.debug(f"Found payment via explicit FK: {payment.payment_number}")
            except Exception as e:
                logger.warning(f"Error fetching payment by FK: {e}")
        
        # 1) SECONDARY MATCH: Tuma request IDs stored in session
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
        
        # 2) FALLBACK: Phone number + amount (legacy, least reliable)
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
        
        # Check payment status
        if payment.status == 'COMPLETED':
            return ('completed', 'Payment successful', payment)
        elif payment.status == 'FAILED':
            return ('failed', payment.failure_reason or 'Payment failed', None)
        else:
            # Check Tuma-specific status if available
            if payment.tuma_status == 'completed' or str(payment.tuma_result_code) == '0':
                # Update payment status if Tuma says completed but our status is still PROCESSING
                if payment.status == 'PROCESSING':
                    payment.status = 'COMPLETED'
                    payment.save()
                return ('completed', 'Payment successful', payment)
            elif payment.tuma_status == 'failed' or (payment.tuma_result_code and str(payment.tuma_result_code) != '0'):
                return ('failed', payment.tuma_result_desc or 'Payment failed', None)
            else:
                return ('pending', 'Waiting for payment confirmation...', None)
    
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

        # Run the entire status check inside the specific ISP's database context
        with schema_context(tenant.schema_name):
            try:
                session = HotspotSession.objects.get(session_id=session_id)
            except HotspotSession.DoesNotExist:
                return Response(
                    {'error': 'Session not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Return current status
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
                # Payment received but not yet activated — activate now
                # Ensure we don't generate a NEW code if one exists
                session.activate(session.access_code)  # <--- Pass current code!
                
                # Create RADIUS credentials
                try:
                    from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                    
                    radius_service = HotspotRadiusService()
                    radius_service.create_hotspot_credentials(
                        username=session.access_code,  # <--- Use session.access_code
                        password=session.access_code,
                        router=session.router,
                        plan=session.plan,
                        expires_at=session.expires_at,
                        mac_address=session.mac_address or '',
                    )
                except Exception as e:
                    logger.error(f"RADIUS activation failed for paid session {session.session_id}: {e}")
                
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
            
            # Check Tuma payment status for pending sessions
            if session.phone_number:
                # Check Tuma payment status
                status, message, payment = self._check_tuma_payment_status(session, tenant.schema_name)
                
                if status == 'completed':
                    # Payment successful - activate session
                    mpesa_receipt = payment.mpesa_receipt if payment else ''
                    session.mark_paid(mpesa_receipt)
                    
                    # Store Tuma reference IDs on session for future lookups
                    if payment:
                        session.tuma_checkout_request_id = payment.tuma_checkout_request_id
                        session.tuma_merchant_request_id = payment.tuma_merchant_request_id
                        session.payment = payment
                        session.save(update_fields=['tuma_checkout_request_id', 'tuma_merchant_request_id', 'payment'])
                    
                    # Activate session (generates access code + expiry)
                    session.activate(session.access_code)
                    
                    # ── CLOUD CONTROLLER: Create RADIUS credentials ──
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
                    # ── END CLOUD CONTROLLER ──
                    
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
                    # Still pending - keep waiting
                    logger.debug(f"Hotspot payment pending for session {session_id}: {message}")
            
            # Still pending
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
        "pin": "1234",           // optional
        "router_id": 5,
        "plan_id": "uuid-...",
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
            pin = (request.data.get('pin') or '').strip()
            router_id = request.data.get('router_id')
            plan_id = request.data.get('plan_id')
            mac_address = (request.data.get('mac_address') or '00:00:00:00:00:00').upper().replace('-', ':')

            if not voucher_code:
                return Response({'error': 'Voucher code is required'}, status=status.HTTP_400_BAD_REQUEST)
            if not router_id or not plan_id:
                return Response({'error': 'Router and plan are required'}, status=status.HTTP_400_BAD_REQUEST)

            # 1. Find and validate voucher
            try:
                voucher = Voucher.objects.select_related('batch').get(code__iexact=voucher_code)
            except Voucher.DoesNotExist:
                return Response({'error': 'Invalid voucher code'}, status=status.HTTP_404_NOT_FOUND)

            if pin and voucher.pin and voucher.pin != pin:
                return Response({'error': 'Invalid PIN'}, status=status.HTTP_400_BAD_REQUEST)

            if not voucher.is_valid():
                reason = 'Voucher has expired' if voucher.status == 'EXPIRED' else \
                         'Voucher has already been used' if voucher.status in ('USED', 'REDEEMED') else \
                         'Voucher is not available'
                return Response({'error': reason}, status=status.HTTP_400_BAD_REQUEST)

            # 2. Find Router & Plan
            try:
                router = Router.objects.get(id=router_id, is_active=True)
            except (Router.DoesNotExist, ValueError):
                return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)

            try:
                plan = HotspotPlan.objects.get(id=plan_id, router=router, is_active=True)
            except HotspotPlan.DoesNotExist:
                return Response({'error': 'Plan not found'}, status=status.HTTP_404_NOT_FOUND)

            # 3. Check voucher value covers the plan price
            if voucher.remaining_value is not None and voucher.remaining_value < plan.price:
                return Response({
                    'error': f'Voucher balance (KES {voucher.remaining_value}) is insufficient for this plan (KES {plan.price})'
                }, status=status.HTTP_400_BAD_REQUEST)

            # 4. Persistent identity (same pattern as purchase flow)
            existing_user = HotspotSession.objects.filter(
                mac_address=mac_address
            ).exclude(access_code__isnull=True).order_by('-created_at').first()

            if existing_user and existing_user.access_code:
                friendly_username = existing_user.access_code
            else:
                friendly_username = self._generate_code()

            # 5. Mark voucher as used
            voucher.use_count = (voucher.use_count or 0) + 1
            if voucher.remaining_value is not None:
                voucher.remaining_value = max(Decimal('0'), voucher.remaining_value - plan.price)
            if not voucher.is_reusable or voucher.use_count >= (voucher.max_uses or 1):
                voucher.status = 'USED'
            voucher.save()

            # 6. Create hotspot session
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
            )

            # 7. Activate & create RADIUS credentials
            try:
                session.activate(friendly_username)
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
                logger.info(f"VOUCHER REDEEM: {voucher.code} -> user {friendly_username} at {router.name}")
            except Exception as e:
                logger.error(f"RADIUS activation failed for voucher: {e}")
                return Response({'error': 'Activation failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            return Response({
                'status': 'success',
                'message': 'Voucher redeemed! You are connected.',
                'access_code': friendly_username,
                'username': friendly_username,
                'password': friendly_username,
                'expires_at': session.expires_at,
                'plan_name': plan.name,
                'remaining_voucher_value': str(voucher.remaining_value) if voucher.remaining_value is not None else None,
            })