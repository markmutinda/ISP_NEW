"""
Hotspot Admin Views for Managing Hotspot Plans, Sessions, and Branding

These are AUTHENTICATED endpoints for ISP staff to manage hotspot configuration.
"""

import logging
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404
from django.db.models import Sum, Count, Q
from django.utils import timezone
from datetime import timedelta

from apps.billing.models.hotspot_models import HotspotPlan, HotspotSession, HotspotBranding
from apps.billing.serializers.hotspot_serializers import (
    HotspotPlanSerializer,
    HotspotSessionSerializer,
    HotspotBrandingSerializer,
)
from apps.network.models.router_models import Router
from apps.core.permissions import IsAdminOrStaff
from utils.pagination import StandardResultsSetPagination

logger = logging.getLogger(__name__)


class HotspotPlanViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing hotspot plans (admin only).
    
    Plans are scoped to routers.
    
    Endpoints:
    - GET    /api/v1/hotspot/routers/{router_id}/plans/
    - POST   /api/v1/hotspot/routers/{router_id}/plans/
    - GET    /api/v1/hotspot/routers/{router_id}/plans/{id}/
    - PATCH  /api/v1/hotspot/routers/{router_id}/plans/{id}/
    - DELETE /api/v1/hotspot/routers/{router_id}/plans/{id}/
    """
    
    serializer_class = HotspotPlanSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    ordering_fields = ['sort_order', 'price', 'name', 'created_at']
    ordering = ['sort_order', 'price']
    
    def get_queryset(self):
        router_id = self.kwargs.get('router_id')
        return HotspotPlan.objects.filter(router_id=router_id)
    
    def get_router(self):
        router_id = self.kwargs.get('router_id')
        return get_object_or_404(Router, id=router_id)
    
    def perform_create(self, serializer):
        router = self.get_router()
        serializer.save(
            router=router,
            created_by=self.request.user
        )
    
    @action(detail=False, methods=['post'])
    def reorder(self, request, router_id=None):
        """
        Reorder plans.
        
        POST /api/v1/hotspot/routers/{router_id}/plans/reorder/
        {
            "order": [{"id": "uuid1", "sort_order": 0}, {"id": "uuid2", "sort_order": 1}]
        }
        """
        order_data = request.data.get('order', [])
        
        for item in order_data:
            plan_id = item.get('id')
            sort_order = item.get('sort_order')
            
            if plan_id and sort_order is not None:
                HotspotPlan.objects.filter(
                    id=plan_id, 
                    router_id=router_id
                ).update(sort_order=sort_order)
        
        return Response({'status': 'Plans reordered'})
    
    @action(detail=True, methods=['post'])
    def toggle_active(self, request, router_id=None, pk=None):
        """Toggle plan active status"""
        plan = self.get_object()
        plan.is_active = not plan.is_active
        plan.save()
        
        return Response({
            'id': str(plan.id),
            'is_active': plan.is_active
        })


class HotspotSessionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for viewing and managing hotspot sessions (admin only).
    
    Sessions are scoped to routers.
    
    Endpoints:
    - GET /api/v1/hotspot/routers/{router_id}/sessions/
    - GET /api/v1/hotspot/routers/{router_id}/sessions/{id}/
    """
    
    serializer_class = HotspotSessionSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status']
    search_fields = ['phone_number', 'mac_address', 'session_id']
    ordering_fields = ['created_at', 'activated_at', 'expires_at', 'amount']
    ordering = ['-created_at']
    pagination_class = StandardResultsSetPagination
    
    def get_queryset(self):
        router_id = self.kwargs.get('router_id')
        return HotspotSession.objects.filter(router_id=router_id).select_related('plan')
    
    @action(detail=True, methods=['post'])
    def disconnect(self, request, router_id=None, pk=None):
        """
        Disconnect/terminate an active session.
        
        POST /api/v1/hotspot/routers/{router_id}/sessions/{id}/disconnect/
        """
        session = self.get_object()
        
        if session.status != 'active':
            return Response(
                {'error': 'Session is not active'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        session.mark_expired()
        
        # TODO: Send disconnect command to MikroTik router
        # This would call the RouterOS API to disconnect the user
        
        return Response({'status': 'Session disconnected'})
    
    @action(detail=False, methods=['get'])
    def stats(self, request, router_id=None):
        """
        Get session statistics for a router.
        
        GET /api/v1/hotspot/routers/{router_id}/sessions/stats/
        """
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=7)
        month_start = today_start - timedelta(days=30)
        
        sessions = HotspotSession.objects.filter(router_id=router_id)
        
        # Active sessions
        active_count = sessions.filter(
            status='active',
            expires_at__gt=now
        ).count()
        
        # Today stats
        today_sessions = sessions.filter(created_at__gte=today_start)
        today_paid = today_sessions.filter(
            status__in=['active', 'paid', 'expired']
        )
        
        # Revenue stats
        today_revenue = today_paid.aggregate(total=Sum('amount'))['total'] or 0
        week_revenue = sessions.filter(
            created_at__gte=week_start,
            status__in=['active', 'paid', 'expired']
        ).aggregate(total=Sum('amount'))['total'] or 0
        month_revenue = sessions.filter(
            created_at__gte=month_start,
            status__in=['active', 'paid', 'expired']
        ).aggregate(total=Sum('amount'))['total'] or 0
        
        # Session counts
        today_count = today_paid.count()
        week_count = sessions.filter(
            created_at__gte=week_start,
            status__in=['active', 'paid', 'expired']
        ).count()
        month_count = sessions.filter(
            created_at__gte=month_start,
            status__in=['active', 'paid', 'expired']
        ).count()
        
        # Popular plans
        popular_plans = sessions.filter(
            created_at__gte=month_start,
            status__in=['active', 'paid', 'expired']
        ).values('plan__name').annotate(
            count=Count('id'),
            revenue=Sum('amount')
        ).order_by('-count')[:5]
        
        return Response({
            'active_sessions': active_count,
            'today': {
                'sessions': today_count,
                'revenue': float(today_revenue),
            },
            'week': {
                'sessions': week_count,
                'revenue': float(week_revenue),
            },
            'month': {
                'sessions': month_count,
                'revenue': float(month_revenue),
            },
            'popular_plans': list(popular_plans),
        })


class HotspotBrandingView(APIView):
    """
    View for managing hotspot branding per router (admin only).
    
    Endpoints:
    - GET   /api/v1/hotspot/routers/{router_id}/branding/
    - PATCH /api/v1/hotspot/routers/{router_id}/branding/
    - PUT   /api/v1/hotspot/routers/{router_id}/branding/
    """
    
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def get_object(self, router_id):
        router = get_object_or_404(Router, id=router_id)
        branding, created = HotspotBranding.objects.get_or_create(
            router=router,
            defaults={
                'company_name': router.name,
                'welcome_title': f'Welcome to {router.name}',
            }
        )
        return branding
    
    def get(self, request, router_id):
        branding = self.get_object(router_id)
        serializer = HotspotBrandingSerializer(branding)
        return Response(serializer.data)
    
    def patch(self, request, router_id):
        branding = self.get_object(router_id)
        serializer = HotspotBrandingSerializer(
            branding, 
            data=request.data, 
            partial=True
        )
        
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    def put(self, request, router_id):
        branding = self.get_object(router_id)
        serializer = HotspotBrandingSerializer(
            branding, 
            data=request.data
        )
        
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class HotspotDashboardView(APIView):
    """
    Global hotspot dashboard stats across all routers (admin only).
    
    GET /api/v1/hotspot/dashboard/
    """
    
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def get(self, request):
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Router stats
        routers = Router.objects.filter(is_active=True)
        routers_with_plans = Router.objects.filter(
            hotspot_plans__isnull=False
        ).distinct().count()
        
        # Session stats
        sessions = HotspotSession.objects.all()
        
        active_sessions = sessions.filter(
            status='active',
            expires_at__gt=now
        ).count()
        
        today_sessions = sessions.filter(created_at__gte=today_start)
        today_paid = today_sessions.filter(
            status__in=['active', 'paid', 'expired']
        )
        
        today_revenue = today_paid.aggregate(total=Sum('amount'))['total'] or 0
        today_count = today_paid.count()
        
        # Total revenue
        total_revenue = sessions.filter(
            status__in=['active', 'paid', 'expired']
        ).aggregate(total=Sum('amount'))['total'] or 0
        
        # Total plans
        total_plans = HotspotPlan.objects.filter(is_active=True).count()
        
        return Response({
            'routers': {
                'total': routers.count(),
                'with_hotspot': routers_with_plans,
            },
            'sessions': {
                'active': active_sessions,
                'today': today_count,
            },
            'revenue': {
                'today': float(today_revenue),
                'total': float(total_revenue),
            },
            'plans': {
                'total': total_plans,
            }
        })
