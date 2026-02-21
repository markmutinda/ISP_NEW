"""
Hotspot Views for Captive Portal Payments

These are PUBLIC endpoints - no authentication required.
End users access these when connecting to WiFi hotspots.
"""

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction, connection
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import Tenant
from apps.billing.models.hotspot_models import HotspotPlan, HotspotSession
from apps.billing.services.payhero import PayHeroClient, PayHeroError
from apps.network.models.router_models import Router

logger = logging.getLogger(__name__)


class CaptivePortalConfigView(APIView):
    """
    Get hotspot portal settings and plans for a specific router.
    
    PUBLIC ENDPOINT - No authentication required.
    GET /api/v1/hotspot/captive-portal/?router={router_id}&tenant={tenant}
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    
    def get(self, request):
        router_id = request.query_params.get('router')
        tenant_subdomain = request.query_params.get('tenant')

        if not router_id or not tenant_subdomain:
            return Response({'status': 'error', 'message': 'Missing router or tenant parameters'}, status=400)

        # 1. Switch to the correct ISP's database schema
        try:
            tenant = Tenant.objects.get(subdomain=tenant_subdomain)
            connection.set_tenant(tenant)
        except Tenant.DoesNotExist:
            return Response({'status': 'error', 'message': 'Invalid ISP tenant'}, status=404)

        # 2. Fetch the router to get its UI settings
        try:
            router = Router.objects.get(id=router_id, is_active=True)
        except Router.DoesNotExist:
            return Response({'status': 'error', 'message': 'Router not found'}, status=404)

        # 3. Get active plans for this router
        plans = HotspotPlan.objects.filter(
            router=router,
            is_active=True
        ).order_by('price')
        
        plans_data = []
        for plan in plans:
            plans_data.append({
                'id': str(plan.id),
                'name': plan.name,
                'price': float(plan.price),
                'download_speed': str(plan.speed_limit_mbps),
                'download_unit': 'Mbps',
                'validity': str(plan.duration_minutes),
                'validity_unit': 'Minutes',
                'description': plan.description,
            })
            
        # Safely get a fallback name if the Company record is missing
        try:
            fallback_name = tenant.company.name
        except Exception:
            fallback_name = str(tenant.subdomain).capitalize()

        # 4. Construct the UI Configuration payload
        portal_config = {
            'template_id': getattr(router, 'template_id', 1),
            'hotspot_name': getattr(router, 'hotspot_name', None) or fallback_name,
            'support_phone': getattr(router, 'support_phone', ''),
            'announcement_text': getattr(router, 'announcement_text', ''),
            'gateway_ip': getattr(router, 'gateway_ip', '')
        }

        return Response({
            'status': 'success',
            'portal_config': portal_config,
            'plans': plans_data
        })


class HotspotPurchaseView(APIView):
    """
    Initiate hotspot purchase via PayHero.
    
    PUBLIC ENDPOINT - No authentication required.
    POST /api/v1/hotspot/pay/
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    
    @transaction.atomic
    def post(self, request):
        router_id = request.data.get('router_id')
        plan_id = request.data.get('plan_id')
        phone_number = request.data.get('phone')
        mac_address = request.data.get('mac_address', '')
        tenant_subdomain = request.data.get('tenant')
        
        if not all([router_id, plan_id, phone_number, tenant_subdomain]):
            return Response({
                'status': 'error',
                'message': 'Missing required fields'
            }, status=status.HTTP_400_BAD_REQUEST)
            
        # Switch Schema
        try:
            tenant = Tenant.objects.get(subdomain=tenant_subdomain)
            connection.set_tenant(tenant)
        except Tenant.DoesNotExist:
            return Response({'status': 'error', 'message': 'Invalid ISP'}, status=404)
        
        try:
            router = Router.objects.get(id=router_id, is_active=True)
            plan = HotspotPlan.objects.get(id=plan_id, router=router, is_active=True)
        except (Router.DoesNotExist, HotspotPlan.DoesNotExist):
            return Response({'status': 'error', 'message': 'Router or Plan not found'}, status=404)
        
        mac_address = mac_address.upper().replace('-', ':')
        session_id = HotspotSession.generate_session_id()
        
        session = HotspotSession.objects.create(
            session_id=session_id,
            router=router,
            plan=plan,
            phone_number=phone_number,
            mac_address=mac_address,
            amount=plan.price,
            status='pending',
        )
        
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
                return Response({
                    'status': 'pending',
                    'transaction_id': response.checkout_request_id,
                    'message': f'M-Pesa prompt sent to {phone_number}. Please enter your PIN.'
                })
            else:
                session.mark_failed(response.message)
                return Response({'status': 'failed', 'message': response.message}, status=400)
                
        except PayHeroError as e:
            session.mark_failed(str(e))
            return Response({'status': 'error', 'message': 'Payment service unavailable.'}, status=500)


class HotspotPurchaseStatusView(APIView):
    """
    Poll hotspot purchase status.
    
    PUBLIC ENDPOINT - No authentication required.
    GET /api/v1/hotspot/pay/status/?transaction_id={id}&tenant={tenant}
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    
    def get(self, request):
        transaction_id = request.query_params.get('transaction_id')
        tenant_subdomain = request.query_params.get('tenant')
        
        if not transaction_id or not tenant_subdomain:
            return Response({'status': 'error', 'message': 'Missing parameters'}, status=400)
            
        # Switch Schema
        try:
            tenant = Tenant.objects.get(subdomain=tenant_subdomain)
            connection.set_tenant(tenant)
        except Tenant.DoesNotExist:
            return Response({'status': 'error', 'message': 'Invalid ISP'}, status=404)
            
        try:
            session = HotspotSession.objects.get(payhero_checkout_id=transaction_id)
        except HotspotSession.DoesNotExist:
            return Response({'status': 'error', 'message': 'Session not found'}, status=404)
            
        if session.status == 'active':
            return Response({'status': 'success', 'message': 'Payment received! Connecting...'})
        elif session.status == 'failed':
            return Response({'status': 'failed', 'message': session.failure_reason or 'Payment failed.'})
        
        # If still pending in our DB, ask PayHero for real-time status
        try:
            from apps.billing.services.payhero import PayHeroClient, PaymentStatus
            client = PayHeroClient()
            status_response = client.get_payment_status(session.payhero_checkout_id)
            
            if status_response.status == PaymentStatus.SUCCESS:
                session.mark_paid(status_response.mpesa_receipt)
                session.activate()
                
                # Create RADIUS credentials for MikroTik
                try:
                    from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                    radius_service = HotspotRadiusService()
                    radius_service.create_hotspot_credentials(
                        username=session.mac_address, # Use MAC as username for seamless login
                        password=session.mac_address,
                        router=session.router,
                        plan=session.plan,
                        expires_at=session.expires_at,
                        mac_address=session.mac_address,
                    )
                except Exception as e:
                    logger.error(f"RADIUS activation failed: {e}")
                    
                return Response({'status': 'success', 'message': 'Payment received! Connecting...'})
                
            elif status_response.status == PaymentStatus.FAILED:
                session.mark_failed(status_response.failure_reason)
                return Response({'status': 'failed', 'message': status_response.failure_reason})
                
        except PayHeroError:
            pass
            
        return Response({'status': 'pending', 'message': 'Waiting for payment confirmation...'})