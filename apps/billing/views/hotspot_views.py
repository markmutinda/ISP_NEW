"""
Hotspot Views for Captive Portal Payments

These are PUBLIC endpoints - no authentication required.
End users access these when connecting to WiFi hotspots.
"""

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from django_tenants.utils import schema_context, get_public_schema_name

from apps.billing.models.hotspot_models import HotspotPlan, HotspotSession, HotspotBranding
from apps.billing.services.payhero import PayHeroClient, PayHeroError
from apps.network.models.router_models import Router
from apps.subscriptions.models import CommissionLedger

logger = logging.getLogger(__name__)


class CaptivePortalView(APIView):
    """
    Public captive-portal endpoint — returns portal config + plans for a
    given router, resolving the tenant explicitly via query parameters.

    GET /api/v1/hotspot/captive-portal/?router={router_id}&tenant={tenant}

    This is the canonical endpoint for the public WiFi captive portal pages.
    It does NOT require authentication.
    """

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        router_id = request.query_params.get('router')
        tenant_subdomain = request.query_params.get('tenant')

        # DEBUG FIX: Print exactly what arrived from the frontend!
        if not router_id or not tenant_subdomain:
            return Response(
                {'message': f'PORTAL CRASH -> Router: "{router_id}", Tenant: "{tenant_subdomain}"'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Resolve the tenant from the public schema ──
        try:
            from apps.core.models import Tenant
            with schema_context(get_public_schema_name()):
                tenant = Tenant.objects.get(subdomain=tenant_subdomain, is_active=True)
        except Exception:
            return Response(
                {'message': f'Tenant not found: {tenant_subdomain}'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # ── Query router + plans inside the tenant schema ──
        try:
            with schema_context(tenant.schema_name):
                # BULLETPROOF: Try by ID, fallback to Name
                try:
                    router = Router.objects.get(id=router_id, is_active=True)
                except (Router.DoesNotExist, ValueError):
                    try:
                        router = Router.objects.get(name=router_id, is_active=True)
                        logger.info(f"Router found by name in CaptivePortalView: {router_id} -> {router.id}")
                    except Router.DoesNotExist:
                        return Response(
                            {'message': f'Router not found: {router_id}'},
                            status=status.HTTP_404_NOT_FOUND,
                        )

                plans = HotspotPlan.objects.filter(
                    router=router,
                    is_active=True,
                ).order_by('sort_order', 'price')

                plans_data = [
                    {
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
                        # Display flags
                        'is_popular': plan.is_popular,
                    }
                    for plan in plans
                ]

                portal_config = {
                    'template_id': router.template_id or 1,
                    'hotspot_name': router.hotspot_name or router.name,
                    'support_phone': router.support_phone or '',
                    'announcement_text': router.announcement_text or '',
                    'gateway_ip': router.gateway_ip,
                }

        except Exception as exc:
            logger.exception('CaptivePortalView error: %s', exc)
            return Response(
                {'message': 'Internal server error'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({
            'status': 'success',
            'portal_config': portal_config,
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
    Initiate hotspot purchase via PayHero.
    
    PUBLIC ENDPOINT - No authentication required.
    
    POST /api/v1/hotspot/purchase/
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []  # No auth required
    
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

        # Run the entire purchase block inside the specific ISP's database context
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
            
            # BULLETPROOF: Get router by ID or name
            try:
                router = Router.objects.get(id=router_id, is_active=True)
            except (Router.DoesNotExist, ValueError):
                try:
                    router = Router.objects.get(name=router_id, is_active=True)
                    logger.info(f"Router found by name in HotspotPurchaseView: {router_id} -> {router.id}")
                except Router.DoesNotExist:
                    return Response(
                        {'error': 'Router not found'},
                        status=status.HTTP_404_NOT_FOUND
                    )
            
            try:
                plan = HotspotPlan.objects.get(id=plan_id, router=router, is_active=True)
            except HotspotPlan.DoesNotExist:
                return Response(
                    {'error': 'Plan not found for this router'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Normalize MAC address
            mac_address = mac_address.upper().replace('-', ':')
            
            # Generate unique session ID
            session_id = HotspotSession.generate_session_id()
            
            # Create pending session
            session = HotspotSession.objects.create(
                session_id=session_id,
                router=router,
                plan=plan,
                phone_number=phone_number,
                mac_address=mac_address,
                amount=plan.price,
                status='pending',
            )
            
            # Initiate PayHero STK Push
            try:
                client = PayHeroClient()
                
                response = client.stk_push(
                    phone_number=phone_number,
                    amount=int(plan.price),
                    reference=session_id,
                    description=f"WiFi Access - {plan.name}",
                    callback_url=settings.PAYHERO_HOTSPOT_CALLBACK,
                )
                
                if response.success:
                    session.payhero_checkout_id = response.checkout_request_id
                    session.save()
                    
                    # Mask phone number for display
                    masked_phone = phone_number[:4] + '***' + phone_number[-3:]
                    
                    return Response({
                        'status': 'pending',
                        'session_id': session_id,
                        'checkout_request_id': response.checkout_request_id,
                        'message': f'STK Push sent to {masked_phone}. Enter your M-Pesa PIN.',
                        'expires_in': 120,  # STK expires in 2 minutes
                    })
                else:
                    session.mark_failed(response.message)
                    
                    return Response({
                        'status': 'error',
                        'message': response.message or 'Failed to initiate payment',
                    }, status=status.HTTP_400_BAD_REQUEST)
            
            except PayHeroError as e:
                logger.error(f"Hotspot PayHero error: {e.message}")
                session.mark_failed(str(e))
                
                return Response({
                    'status': 'error',
                    'message': 'Payment service unavailable. Please try again.',
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class HotspotPurchaseStatusView(APIView):
    """
    Poll hotspot purchase status.
    
    PUBLIC ENDPOINT - No authentication required.
    
    GET /api/v1/hotspot/purchase/{session_id}/status/
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []  # No auth required
    
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
                session.activate()
                
                # Create RADIUS credentials
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
            
            # Still pending - check with PayHero
            if session.payhero_checkout_id:
                try:
                    from apps.billing.services.payhero import PayHeroClient, PaymentStatus
                    
                    client = PayHeroClient()
                    status_response = client.get_payment_status(session.payhero_checkout_id)
                    
                    if status_response.status == PaymentStatus.SUCCESS:
                        # Payment successful - activate session
                        session.mark_paid(status_response.mpesa_receipt)
                        
                        # Activate session (generates access code + expiry)
                        session.activate()
                        
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
                    
                    elif status_response.status == PaymentStatus.FAILED:
                        session.mark_failed(status_response.failure_reason)
                        return Response({
                            'status': 'failed',
                            'message': status_response.failure_reason or 'Payment failed. Please try again.',
                        })
                
                except PayHeroError as e:
                    logger.error(f"Error checking hotspot payment status: {e.message}")
            
            # Still pending
            return Response({
                'status': 'pending',
                'message': 'Waiting for payment confirmation...',
            })