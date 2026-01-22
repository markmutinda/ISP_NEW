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

from django_tenants.utils import schema_context

from apps.billing.models.hotspot_models import HotspotPlan, HotspotSession, HotspotBranding
from apps.billing.services.payhero import PayHeroClient, PayHeroError
from apps.network.models.router_models import Router
from apps.subscriptions.models import CommissionLedger

logger = logging.getLogger(__name__)


class HotspotPlansView(APIView):
    """
    Get hotspot plans for a specific router.
    
    PUBLIC ENDPOINT - No authentication required.
    
    GET /api/v1/hotspot/routers/{router_id}/plans/
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []  # No auth required
    
    def get(self, request, router_id):
        try:
            router = Router.objects.get(id=router_id, is_active=True)
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
        
        return Response({
            'router': {
                'id': router.id,
                'name': router.name,
                'location': router.location,
            },
            'plans': plans_data,
            'branding': branding_data,
        })


class HotspotPurchaseView(APIView):
    """
    Initiate hotspot purchase via PayHero.
    
    PUBLIC ENDPOINT - No authentication required.
    
    POST /api/v1/hotspot/purchase/
    {
        "router_id": 5,
        "plan_id": "uuid",
        "phone_number": "254712345678",
        "mac_address": "AA:BB:CC:DD:EE:FF"
    }
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []  # No auth required
    
    @transaction.atomic
    def post(self, request):
        router_id = request.data.get('router_id')
        plan_id = request.data.get('plan_id')
        phone_number = request.data.get('phone_number')
        mac_address = request.data.get('mac_address', '')
        
        # Validate required fields
        if not all([router_id, plan_id, phone_number]):
            return Response({
                'error': 'Missing required fields: router_id, plan_id, phone_number'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get router and plan
        try:
            router = Router.objects.get(id=router_id, is_active=True)
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
            # Payment received, activating on router
            return Response({
                'status': 'activating',
                'message': 'Payment received! Activating your connection...',
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
                    
                    # Activate on router (in production this would call MikroTik API)
                    # For now, we just set access code
                    session.activate()
                    
                    # Record commission
                    # Note: Need to get company from router's tenant
                    # This is handled in webhook for proper tenant context
                    
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
