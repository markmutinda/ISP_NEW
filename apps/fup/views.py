import csv

from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import Plan, HotspotPlan
from apps.core.permissions import IsAdmin, IsAdminOrStaff
from apps.customers.models import ServiceConnection

from .models import (
    FUPPolicy,
    FUPPolicyPlan,
    FUPPolicyHotspotPlan,
    FUPViolation,
    FUPThrottleState,
)
from .serializers import (
    FUPPolicySerializer,
    FUPPolicyPlanSerializer,
    FUPViolationSerializer,
    FUPThrottleStateSerializer,
    FUPDashboardSummarySerializer,
    FUPAnalyticsOverviewSerializer,
    AvailablePlanSerializer,
    LinkPlansSerializer,
)
from .services import (
    FUPEnforcementService,
    FUPAnalyticsService,
)


class FUPDashboardSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        active_policies = FUPPolicy.objects.filter(is_active=True, status='ACTIVE').count()
        users_under_fup = ServiceConnection.objects.filter(
            status='ACTIVE',
            plan__fup_policy_links__policy__is_active=True,
            plan__fup_policy_links__policy__status='ACTIVE',
            plan__fup_policy_links__is_active=True,
        ).distinct().count()
        active_violations = FUPViolation.objects.filter(status='OPEN').count()
        currently_throttled = FUPThrottleState.objects.filter(active=True).count()

        serializer = FUPDashboardSummarySerializer({
            'active_policies': active_policies,
            'users_under_fup': users_under_fup,
            'active_violations': active_violations,
            'currently_throttled': currently_throttled,
        })
        return Response(serializer.data)


class FUPPolicyViewSet(viewsets.ModelViewSet):
    queryset = FUPPolicy.objects.all().order_by('-created_at')
    serializer_class = FUPPolicySerializer
    permission_classes = [IsAuthenticated, IsAdmin | IsAdminOrStaff]

    def perform_create(self, serializer):
        serializer.save(
            created_by=self.request.user,
            updated_by=self.request.user,
        )

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    @action(detail=True, methods=['get'])
    def linked_plans(self, request, pk=None):
        policy = self.get_object()
        
        # Billing Plans
        billing_links = policy.plan_links.select_related('plan').filter(is_active=True)
        # Hotspot Plans
        hotspot_links = policy.hotspot_plan_links.select_related('hotspot_plan').filter(is_active=True)

        return Response({
            'billing_plans': [
                {
                    'id': link.plan.id, 
                    'name': link.plan.name,
                    'plan_type': getattr(link.plan, 'plan_type', None),
                } 
                for link in billing_links
            ],
            'hotspot_plans': [
                {
                    'id': link.hotspot_plan.id, 
                    'name': link.hotspot_plan.name,
                } 
                for link in hotspot_links
            ]
        })

    @action(detail=True, methods=['get'])
    def available_plans(self, request, pk=None):
        policy = self.get_object()
        
        # Existing Billing Plans
        linked_billing_ids = set(policy.plan_links.values_list('plan_id', flat=True))
        billing_plans = Plan.objects.filter(is_active=True).annotate(
            subscriber_count=Count('service_connections')
        ).order_by('name')
        
        # Hotspot Plans
        linked_hotspot_ids = set(policy.hotspot_plan_links.values_list('hotspot_plan_id', flat=True))
        hotspot_plans = HotspotPlan.objects.filter(is_active=True).order_by('name')

        return Response({
            'billing_plans': [
                {
                    'id': plan.id,
                    'name': plan.name,
                    'plan_type': plan.plan_type,
                    'subscriber_count': plan.subscriber_count,
                    'already_linked': plan.id in linked_billing_ids,
                }
                for plan in billing_plans
            ],
            'hotspot_plans': [
                {
                    'id': plan.id,
                    'name': plan.name,
                    'already_linked': plan.id in linked_hotspot_ids,
                }
                for plan in hotspot_plans
            ]
        })

    @action(detail=True, methods=['post'])
    def link_plans(self, request, pk=None):
        policy = self.get_object()
        serializer = LinkPlansSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Track counts for response
        billing_created = 0
        billing_reactivated = 0
        hotspot_created = 0
        hotspot_reactivated = 0

        # 1. Process Billing Plans
        for plan_id in serializer.validated_data.get('plan_ids', []):
            link, created = FUPPolicyPlan.objects.get_or_create(
                policy=policy,
                plan_id=plan_id,
                defaults={
                    'is_active': True,
                    'linked_by': request.user,
                }
            )
            
            if created:
                billing_created += 1
            elif not link.is_active:
                # Reactivate the existing inactive link
                link.is_active = True
                link.linked_by = request.user
                link.save(update_fields=['is_active', 'linked_by'])
                billing_reactivated += 1

        # 2. Process Hotspot Plans
        for hotspot_id in serializer.validated_data.get('hotspot_plan_ids', []):
            link, created = FUPPolicyHotspotPlan.objects.get_or_create(
                policy=policy,
                hotspot_plan_id=hotspot_id,
                defaults={
                    'is_active': True,
                    'linked_by': request.user,
                }
            )
            
            if created:
                hotspot_created += 1
            elif not link.is_active:
                # Reactivate the existing inactive link
                link.is_active = True
                link.linked_by = request.user
                link.save(update_fields=['is_active', 'linked_by'])
                hotspot_reactivated += 1

        return Response({
            'message': 'Plans processed successfully.',
            'billing_plans': {
                'created': billing_created,
                'reactivated': billing_reactivated,
            },
            'hotspot_plans': {
                'created': hotspot_created,
                'reactivated': hotspot_reactivated,
            }
        })

    @action(detail=True, methods=['post'])
    def unlink_plans(self, request, pk=None):
        policy = self.get_object()
        serializer = LinkPlansSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Track counts for response
        billing_unlinked = 0
        hotspot_unlinked = 0

        # Unlink Billing Plans
        if serializer.validated_data.get('plan_ids'):
            billing_unlinked = policy.plan_links.filter(
                plan_id__in=serializer.validated_data['plan_ids'],
                is_active=True,
            ).update(is_active=False)

        # Unlink Hotspot Plans
        if serializer.validated_data.get('hotspot_plan_ids'):
            hotspot_unlinked = policy.hotspot_plan_links.filter(
                hotspot_plan_id__in=serializer.validated_data['hotspot_plan_ids'],
                is_active=True,
            ).update(is_active=False)

        return Response({
            'message': 'Plans unlinked successfully.',
            'billing_plans_unlinked': billing_unlinked,
            'hotspot_plans_unlinked': hotspot_unlinked,
        })

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        policy = self.get_object()
        policy.status = 'ACTIVE'
        policy.is_active = True
        policy.updated_by = request.user
        policy.save(update_fields=['status', 'is_active', 'updated_by', 'updated_at'])
        return Response({'message': 'Policy activated successfully.'})

    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        policy = self.get_object()
        policy.status = 'INACTIVE'
        policy.is_active = False
        policy.updated_by = request.user
        policy.save(update_fields=['status', 'is_active', 'updated_by', 'updated_at'])
        return Response({'message': 'Policy deactivated successfully.'})

    @action(detail=True, methods=['post'])
    def run_enforcement(self, request, pk=None):
        policy = self.get_object()
        service = FUPEnforcementService()

        count = 0
        # Process regular billing plans
        for link in policy.plan_links.filter(is_active=True).select_related('plan'):
            services = ServiceConnection.objects.filter(status='ACTIVE', plan=link.plan)
            for svc in services:
                service.evaluate_service(svc)
                count += 1

        # Process hotspot plans (if hotspot service connections exist)
        # Note: You may need to add hotspot service connection logic here
        # This would depend on how hotspot services are linked to customers

        return Response({
            'message': 'Policy enforcement completed.',
            'services_processed': count,
        })


class FUPViolationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = FUPViolation.objects.select_related('customer', 'policy', 'plan').all()
    serializer_class = FUPViolationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()

        policy_id = self.request.query_params.get('policy_id')
        status_value = self.request.query_params.get('status')
        search = self.request.query_params.get('search')

        if policy_id:
            qs = qs.filter(policy_id=policy_id)
        if status_value:
            qs = qs.filter(status=status_value)
        if search:
            qs = qs.filter(customer__customer_code__icontains=search)

        return qs.order_by('-occurred_at')

    @action(detail=False, methods=['get'])
    def export(self, request):
        queryset = self.get_queryset()

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="fup_violations.csv"'

        writer = csv.writer(response)
        writer.writerow(['User', 'Policy', 'Usage(GB)', 'Action', 'Status', 'Occurred'])

        for item in queryset:
            writer.writerow([
                getattr(item.customer, 'customer_code', ''),
                item.policy.name,
                round(item.total_usage_bytes / (1024 ** 3), 2),
                item.action_taken,
                item.status,
                item.occurred_at.isoformat(),
            ])

        return response


class FUPThrottleStateViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = FUPThrottleState.objects.select_related('customer', 'policy').all()
    serializer_class = FUPThrottleStateSerializer
    permission_classes = [IsAuthenticated]


class FUPAnalyticsOverviewView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        data = FUPAnalyticsService().overview()
        serializer = FUPAnalyticsOverviewSerializer(data)
        return Response(serializer.data)