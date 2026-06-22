"""
Hotspot Admin Views for Managing Hotspot Plans, Sessions, and Branding

These are AUTHENTICATED endpoints for ISP staff to manage hotspot configuration.
"""

import logging
from rest_framework import viewsets, status, filters, generics
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404
from django.db.models import Sum, Count, Q
from django.utils import timezone
from datetime import timedelta

from apps.billing.models.hotspot_models import HotspotPlan, HotspotSession, HotspotBranding, HotspotClient
from apps.billing.serializers.hotspot_serializers import (
    HotspotPlanSerializer,
    HotspotSessionSerializer,
    HotspotBrandingSerializer,
    HotspotClientSerializer,
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
    - GET    /api/v1/hotspot/admin/routers/{router_id}/plans/
    - POST   /api/v1/hotspot/admin/routers/{router_id}/plans/
    - GET    /api/v1/hotspot/admin/routers/{router_id}/plans/{id}/
    - PATCH  /api/v1/hotspot/admin/routers/{router_id}/plans/{id}/
    - DELETE /api/v1/hotspot/admin/routers/{router_id}/plans/{id}/
    """
    
    serializer_class = HotspotPlanSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    pagination_class = None  # Plans are few per router, return flat array
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
        """Save hotspot plan with router context and handle duplicate constraints politely."""
        from django.db import IntegrityError
        from rest_framework import serializers
        router = self.get_router()
        
        try:
            serializer.save(
                router=router,
                created_by=self.request.user
            )
        except IntegrityError as e:
            # Catch the unique_together constraint violation for router + name
            if "router" in str(e).lower() and "name" in str(e).lower():
                raise serializers.ValidationError({
                    "name": "A hotspot plan with this name already exists for this specific router."
                })
            # Re-raise any unrelated database integrity issues
            raise e
    
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


class GlobalHotspotPlanListView(generics.ListAPIView):
    """
    Get all active hotspot plans across all routers for dropdowns (like the Vouchers page).
    GET /api/v1/hotspot/admin/plans/
    """
    serializer_class = HotspotPlanSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    queryset = HotspotPlan.objects.filter(is_active=True).select_related('router')
    pagination_class = None
    filter_backends = [filters.OrderingFilter]
    ordering = ['router__name', 'price']


class HotspotClientViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for viewing transient hotspot clients (admin only).
    
    Endpoints:
    - GET /api/v1/hotspot/admin/clients/
    - GET /api/v1/hotspot/admin/clients/{id}/
    """
    
    serializer_class = HotspotClientSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['canonical_phone', 'email', 'external_client_id', 'canonical_username']
    ordering_fields = ['last_seen_at', 'first_seen_at', 'total_spend', 'total_sessions']
    ordering = ['-last_seen_at']
    pagination_class = StandardResultsSetPagination
    
    def get_queryset(self):
        # Prefetch sessions to optimize the nested serializer query
        return HotspotClient.objects.prefetch_related('sessions__plan').all()


class ActiveSubscriptionsView(APIView):
    """
    Returns all currently-active subscriptions across PPPoE and Hotspot.
    Designed so the frontend Active Subs tab can consume a single endpoint.

    Response shape:
    {
        "pppoe": [ { ...PPPoE customer data... } ],
        "hotspot": [ { ...hotspot session data... } ],
        "total": 42
    }
    
    UPDATED: Hotspot tab now shows ALL clients (not just active) with pagination.
    FIXED: Return ALL hotspot clients — frontend handles pagination.
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        from apps.radius.models import RadAcct
        from apps.billing.models.hotspot_models import HotspotSession
        
        now = timezone.now()

        # ── PPPoE / Static active subscriptions ─────────────────────────────
        from apps.radius.models import CustomerRadiusCredentials

        pppoe_creds = (
            CustomerRadiusCredentials.objects
            .filter(is_enabled=True)
            .select_related('customer__user', 'bandwidth_profile')
        )

        pppoe_results = []
        for cred in pppoe_creds:
            is_expired = (
                cred.expiration_date is not None and cred.expiration_date <= now
            )
            if is_expired:
                continue

            customer = cred.customer
            user = customer.user if customer else None

            expiry_str = (
                cred.expiration_date.isoformat()
                if cred.expiration_date else None
            )
            days_left = None
            if cred.expiration_date:
                days_left = max(
                    0,
                    int((cred.expiration_date - now).total_seconds() / 86400)
                )

            # Best-effort plan info
            service = customer.services.filter(status='ACTIVE').first() if customer else None
            plan_name = service.plan.name if (service and service.plan) else "No Plan"
            plan_price = (
                float(service.plan.base_price)
                if (service and service.plan) else 0
            )

            pppoe_results.append({
                "type": "pppoe",
                "username": cred.username,
                "canonical_username": None,          # PPPoE uses real name
                "display_name": (
                    customer.full_name if customer else cred.username
                ),
                "phone": user.phone_number if user else None,
                "email": user.email if user else None,
                "customer_code": customer.customer_code if customer else None,
                "plan_name": plan_name,
                "plan_price": plan_price,
                "expiry_date": expiry_str,
                "days_left": days_left,
                "is_unlimited": cred.expiration_date is None,
                "connection_type": cred.connection_type,
                "subscribed_at": cred.created_at.isoformat() if cred.created_at else None,
            })

        # ── Hotspot subscriptions — ALL clients, any session status ──
        # We group by hotspot_client and show their most recent session
        all_hotspot_sessions = (
            HotspotSession.objects
            .filter(
                status__in=['active', 'paid', 'expired', 'pending'],
                hotspot_client__isnull=False,
            )
            .select_related('plan', 'router', 'hotspot_client')
            .order_by('-activated_at')
        )

        # De-duplicate: one entry per client (most recent session wins)
        seen_clients = set()
        unique_sessions = []
        for s in all_hotspot_sessions:
            cid = s.hotspot_client_id
            if cid not in seen_clients:
                seen_clients.add(cid)
                unique_sessions.append(s)

        # FIX 2: Return ALL hotspot clients — frontend handles pagination
        total_hotspot = len(unique_sessions)
        paginated_sessions = unique_sessions  # Return ALL, frontend handles pagination

        # Build radacct map for enrichment only (not for filtering)
        open_radacct_usernames = set(
            RadAcct.objects.filter(acctstoptime__isnull=True).values_list('username', flat=True)
        )

        hotspot_results = []
        for session in paginated_sessions:
            # Determine online status for display purposes only — don't use it to filter
            has_open_radacct = bool(session.access_code and session.access_code in open_radacct_usernames)
            is_within_startup_grace = (
                session.activated_at
                and session.activated_at >= now - timedelta(minutes=5)
            )
            
            # Show connection status but always include the session if subscription is valid
            if has_open_radacct:
                online_source = "radacct"
            elif is_within_startup_grace:
                online_source = "hotspot_pending_accounting"
            else:
                online_source = "subscription_active"  # valid subscription, radacct may have been swept

            client = session.hotspot_client
            canonical_phone = None
            if client and client.canonical_phone:
                p = client.canonical_phone
                canonical_phone = p if not p.startswith("MAC-") else None

            days_left = max(
                0, int((session.expires_at - now).total_seconds() / 86400)
            ) if session.expires_at else 0
            hours_left = max(
                0, int((session.expires_at - now).total_seconds() / 3600)
            ) if session.expires_at else 0

            # Subscription status
            subscription_status = session.status  # 'active', 'expired', etc.
            is_active_sub = session.status == 'active' and (session.expires_at and session.expires_at > now)

            hotspot_results.append({
                "type": "hotspot",
                "username": session.access_code,           # e.g. "MXA-BKCS"
                "canonical_username": session.access_code, # same thing
                "display_name": session.access_code,       # shown prominently
                "phone": canonical_phone or session.phone_number,
                "email": client.email if client else None,
                "customer_code": None,                     # no formal customer record
                "plan_name": session.plan.name if session.plan else "Unknown",
                "plan_price": float(session.amount),
                "expiry_date": session.expires_at.isoformat() if session.expires_at else None,
                "days_left": days_left,
                "hours_left": hours_left,
                "is_unlimited": False,
                "connection_type": "HOTSPOT",
                "subscribed_at": session.activated_at.isoformat() if session.activated_at else None,
                "router": session.router.name if session.router else None,
                "mac_address": session.mac_address,
                "session_id": session.session_id,
                # FIXED: Use new online_source field
                "online_source": online_source,
                "is_confirmed_online": has_open_radacct,  # frontend can show a dot indicator
                "pending_accounting_start": is_within_startup_grace,
                # Lifetime analytics for this client
                "client_total_sessions": client.total_sessions if client else 1,
                "client_total_spend": float(client.total_spend) if client else float(session.amount),
                # ADDED: Subscription status fields
                "subscription_status": subscription_status,  # 'active', 'expired', etc.
                "is_active_sub": is_active_sub,
                "client_id": session.hotspot_client_id,  # ADD THIS LINE for client detail fetch
            })

        return Response({
            "pppoe": pppoe_results,
            "hotspot": hotspot_results,
            "total": len(pppoe_results) + len(hotspot_results),
            "hotspot_total": total_hotspot,
            "hotspot_page": 1,  # Frontend handles pagination, always return page 1
            "hotspot_page_size": total_hotspot,  # All items returned at once
        })


class HotspotClientDetailView(APIView):
    """
    GET /api/v1/hotspot/admin/clients/{id}/sessions/
    Returns RADIUS sessions for a hotspot client's canonical_username
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request, id):
        from apps.radius.models import RadAcct
        try:
            client = HotspotClient.objects.get(id=id)
        except HotspotClient.DoesNotExist:
            return Response({'error': 'Client not found'}, status=404)

        username = client.canonical_username
        if not username:
            return Response({'sessions': [], 'count': 0})

        sessions_qs = RadAcct.objects.filter(
            username=username
        ).select_related('router').order_by('-acctstarttime')

        # Pagination
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        total = sessions_qs.count()
        start = (page - 1) * page_size
        sessions = sessions_qs[start:start + page_size]

        def fmt_bytes(b):
            if not b: return '0 B'
            b = int(b)
            for unit in ['B', 'KB', 'MB', 'GB']:
                if b < 1024: return f'{b:.2f} {unit}'
                b /= 1024
            return f'{b:.2f} TB'

        data = []
        for s in sessions:
            total_bytes = (s.acctinputoctets or 0) + (s.acctoutputoctets or 0)
            duration_secs = s.acctsessiontime or 0
            if not duration_secs and s.acctstarttime and s.acctstoptime:
                duration_secs = int((s.acctstoptime - s.acctstarttime).total_seconds())
            h, rem = divmod(duration_secs, 3600)
            m, sec = divmod(rem, 60)
            duration_fmt = f'{h:02d}:{m:02d}:{sec:02d}'

            data.append({
                'id': s.radacctid,
                'mac_address': s.callingstationid or '',
                'ip_address': s.framedipaddress or '',
                'router': s.router.name if s.router else (s.nasipaddress or ''),
                'nas_ip': s.nasipaddress or '',
                'start_time': s.acctstarttime.isoformat() if s.acctstarttime else None,
                'stop_time': s.acctstoptime.isoformat() if s.acctstoptime else None,
                'duration': duration_fmt,
                'duration_seconds': duration_secs,
                'data_total': fmt_bytes(total_bytes),
                'data_upload': fmt_bytes(s.acctinputoctets or 0),
                'data_download': fmt_bytes(s.acctoutputoctets or 0),
                'terminate_cause': s.acctterminatecause or '',
                'is_active': s.acctstoptime is None,
            })

        return Response({
            'client': {
                'id': client.id,
                'canonical_username': client.canonical_username,
                'canonical_phone': client.canonical_phone,
                'total_sessions': client.total_sessions,
                'total_spend': float(client.total_spend),
                'first_seen_at': client.first_seen_at.isoformat() if client.first_seen_at else None,
                'last_seen_at': client.last_seen_at.isoformat() if client.last_seen_at else None,
            },
            'sessions': data,
            'count': total,
            'page': page,
            'page_size': page_size,
            'total_pages': (total + page_size - 1) // page_size,
        })


class RouterIncomeView(APIView):
    """
    GET /api/v1/hotspot/admin/routers/{router_id}/income/
    Returns total income generated by a specific router (hotspot + PPPoE).
    Scoped to current tenant — never leaks across routers or tenants.
    
    FIXED: Now uses Payment table as single source of truth instead of
    summing HotspotSession.amount which overcounts due to transitional
    'paid' status and test/pending sessions.
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request, router_id):
        from apps.billing.models.payment_models import Payment
        from apps.network.models.router_models import PPPoEUser
        from django.db.models import Sum

        # Verify router exists and user has access (tenant-scoped via Router lookup)
        router = get_object_or_404(Router, id=router_id)

        # Hotspot income: sum COMPLETED payments linked to hotspot sessions on this router
        hotspot_income = (
            Payment.objects
            .filter(
                status='COMPLETED',
                hotspot_sessions__router_id=router_id,
            )
            .aggregate(total=Sum('amount'))['total'] or 0
        )

        # PPPoE income: completed payments for customers whose active service is on this router
        pppoe_customer_ids = (
            PPPoEUser.objects
            .filter(router_id=router_id, service_connection__isnull=False)
            .values_list('service_connection__customer_id', flat=True)
        )

        pppoe_income = (
            Payment.objects
            .filter(
                status='COMPLETED',
                customer_id__in=pppoe_customer_ids,
                hotspot_sessions__isnull=True,  # exclude hotspot payments
            )
            .aggregate(total=Sum('amount'))['total'] or 0
        )

        total = float(hotspot_income) + float(pppoe_income)

        return Response({
            'router_id': router_id,
            'router_name': router.name,
            'hotspot_income': float(hotspot_income),
            'pppoe_income': float(pppoe_income),
            'total_income': total,
        })


# ============================================================
# HOTSPOT SESSION EXTENSION VIEW (for admin manual extension)
# ============================================================

class HotspotSessionExtendView(APIView):
    """
    Extend an active hotspot session's expiration time.
    
    POST /api/v1/hotspot/admin/sessions/{session_id}/extend/
    
    Request body options:
    {
        "duration_amount": 1,
        "duration_unit": "HOURS",  # MINUTES, HOURS, DAYS
        // OR
        "expiry_date": "2025-12-31T23:59:59"  # ISO format override
    }
    
    Permission: Admin or Staff only.
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def post(self, request, session_id):
        from apps.billing.models.hotspot_models import HotspotSession
        from apps.billing.services.hotspot_radius_service import HotspotRadiusService
        from django.utils.dateparse import parse_datetime

        # Get the session (must be active)
        try:
            session = HotspotSession.objects.get(
                session_id=session_id,
                status='active'
            )
        except HotspotSession.DoesNotExist:
            return Response(
                {'error': 'Active session not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        now = timezone.now()
        
        # Parse extension parameters
        duration_amount = request.data.get('duration_amount', 1)
        duration_unit = request.data.get('duration_unit', 'HOURS').upper()
        expiry_date_str = request.data.get('expiry_date')  # ISO string override
        
        # Determine base time for extension (current expiry if still valid, else now)
        base_time = session.expires_at if (session.expires_at and session.expires_at > now) else now
        
        # Calculate new expiry
        if expiry_date_str:
            new_expiry = parse_datetime(expiry_date_str)
            if not new_expiry:
                return Response(
                    {'error': 'Invalid expiry_date format. Use ISO format e.g., 2025-12-31T23:59:59'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if new_expiry <= now:
                return Response(
                    {'error': 'Expiry date must be in the future'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        else:
            # Validate duration
            try:
                duration_amount = int(duration_amount)
                if duration_amount <= 0:
                    raise ValueError()
            except (TypeError, ValueError):
                return Response(
                    {'error': 'duration_amount must be a positive integer'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Map unit to timedelta
            delta_map = {
                'MINUTES': timedelta(minutes=duration_amount),
                'HOURS': timedelta(hours=duration_amount),
                'DAYS': timedelta(days=duration_amount),
            }
            delta = delta_map.get(duration_unit)
            if not delta:
                return Response(
                    {'error': 'duration_unit must be MINUTES, HOURS, or DAYS'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            new_expiry = base_time + delta
        
        # Update session expiry
        old_expiry = session.expires_at
        session.expires_at = new_expiry
        session.save(update_fields=['expires_at', 'updated_at'])
        
        # ============================================================
        # FIX: Update RADIUS credentials with correct Session-Timeout
        # The extend_session method now calculates remaining seconds
        # from new_expires_at instead of using additional_minutes
        # ============================================================
        try:
            radius_service = HotspotRadiusService()
            
            # Calculate the number of minutes added (for logging only)
            added_minutes = int((new_expiry - base_time).total_seconds() / 60)
            
            # Call extend_session - it will calculate Session-Timeout
            # correctly from new_expires_at
            radius_service.extend_session(
                username=session.access_code,
                additional_minutes=added_minutes,  # Used as fallback only
                new_expires_at=new_expiry,
            )
            
            logger.info(
                f"Session {session.session_id} extended from {old_expiry} to {new_expiry} "
                f"by {request.user.username} (added {added_minutes} minutes)"
            )
        except Exception as e:
            # ============================================================
            # FIX: Even if RADIUS extension fails, the session expiry is
            # already updated in DB. The user will get the new expiry on
            # next re-authentication. Log but don't fail the request.
            # ============================================================
            logger.warning(
                f"RADIUS extension failed for session {session.session_id} "
                f"(session expiry already updated in DB): {e}"
            )
            # Not raising an exception - the DB update is what matters most
        
        # Calculate human-readable extension display
        extension_display = ""
        if not expiry_date_str:
            if duration_unit == 'MINUTES':
                extension_display = f"{duration_amount} minute(s)"
            elif duration_unit == 'HOURS':
                extension_display = f"{duration_amount} hour(s)"
            elif duration_unit == 'DAYS':
                extension_display = f"{duration_amount} day(s)"
        
        return Response({
            'status': 'success',
            'session_id': session_id,
            'old_expiry': old_expiry.isoformat() if old_expiry else None,
            'new_expiry': new_expiry.isoformat(),
            'extension': extension_display,
            'message': f'Session extended to {new_expiry.strftime("%b %d %Y %H:%M")}',
        })