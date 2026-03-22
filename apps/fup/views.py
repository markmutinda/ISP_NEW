import csv

from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import Plan
from apps.core.permissions import IsAdmin, IsAdminOrStaff
from apps.customers.models import ServiceConnection

from .models import (
    FUPPolicy,
    FUPPolicyPlan,
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
        links = policy.plan_links.select_related('plan').filter(is_active=True)
        serializer = FUPPolicyPlanSerializer(links, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def available_plans(self, request, pk=None):
        policy = self.get_object()
        linked_ids = set(policy.plan_links.values_list('plan_id', flat=True))

        plans = Plan.objects.filter(is_active=True).annotate(
            subscriber_count=Count('service_connections')
        ).order_by('name')

        data = [
            {
                'id': plan.id,
                'name': plan.name,
                'plan_type': plan.plan_type,
                'subscriber_count': plan.subscriber_count,
                'already_linked': plan.id in linked_ids,
            }
            for plan in plans
        ]

        serializer = AvailablePlanSerializer(data, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def link_plans(self, request, pk=None):
        policy = self.get_object()
        serializer = LinkPlansSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        created = 0
        for plan_id in serializer.validated_data['plan_ids']:
            _, was_created = FUPPolicyPlan.objects.get_or_create(
                policy=policy,
                plan_id=plan_id,
                defaults={
                    'is_active': True,
                    'linked_by': request.user,
                }
            )
            if was_created:
                created += 1

        return Response({
            'message': 'Plans linked successfully.',
            'created': created,
        })

    @action(detail=True, methods=['post'])
    def unlink_plans(self, request, pk=None):
        policy = self.get_object()
        serializer = LinkPlansSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        updated = policy.plan_links.filter(
            plan_id__in=serializer.validated_data['plan_ids'],
            is_active=True,
        ).update(is_active=False)

        return Response({
            'message': 'Plans unlinked successfully.',
            'updated': updated,
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
        for link in policy.plan_links.filter(is_active=True).select_related('plan'):
            services = ServiceConnection.objects.filter(status='ACTIVE', plan=link.plan)
            for svc in services:
                service.evaluate_service(svc)
                count += 1

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