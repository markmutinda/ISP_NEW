import csv
import logging

from django.db import transaction
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.http import HttpResponse
from django.utils import timezone
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
    FUPUsageWindow,
    FUPAuditLog,
)
from .serializers import (
    FUPPolicySerializer,
    FUPViolationSerializer,
    FUPThrottleStateSerializer,
    FUPUsageWindowSerializer,
    FUPAnalyticsOverviewSerializer,
    LinkPlansSerializer,
)
from .services import (
    FUPEnforcementService,
    FUPAnalyticsService,
)

logger = logging.getLogger(__name__)


class FUPDashboardSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        active_policies = FUPPolicy.objects.filter(is_active=True, status='ACTIVE').count()
        
        # Only count users on ACTIVE policies with ACTIVE links
        users_under_fup = ServiceConnection.objects.filter(
            status='ACTIVE',
            plan__fup_policy_links__is_active=True,
            plan__fup_policy_links__policy__is_active=True,
            plan__fup_policy_links__policy__status='ACTIVE',
        ).distinct().count()
        
        active_violations = FUPViolation.objects.filter(status='OPEN').count()
        currently_throttled = FUPThrottleState.objects.filter(active=True).count()

        return Response({
            'active_policies': active_policies,
            'users_under_fup': users_under_fup,
            'active_violations': active_violations,
            'currently_throttled': currently_throttled,
        })


class FUPPolicyViewSet(viewsets.ModelViewSet):
    queryset = FUPPolicy.objects.all().order_by('-created_at')
    serializer_class = FUPPolicySerializer
    permission_classes = [IsAuthenticated, IsAdmin | IsAdminOrStaff]

    # --- Helper Methods ---

    def _log_policy_event(self, policy, event_type: str, message: str, metadata=None):
        try:
            FUPAuditLog.objects.create(
                policy=policy,
                event_type=event_type,
                message=message,
                metadata=metadata or {},
                created_by=self.request.user,
            )
        except Exception:
            pass

    def _release_all_throttles_for_policy(self, policy, reason: str):
        service = FUPEnforcementService()
        released = 0
        throttle_states = FUPThrottleState.objects.filter(policy=policy, active=True).select_related('service_connection')
        
        for ts in throttle_states:
            if service.release_service(ts.service_connection, reason=reason):
                released += 1
        return released

    def _evaluate_all_linked_services_for_policy(self, policy):
        service = FUPEnforcementService()
        processed = 0
        throttled = 0
        
        for link in policy.plan_links.filter(is_active=True).select_related('plan'):
            svcs = ServiceConnection.objects.filter(status='ACTIVE', plan=link.plan)
            for svc in svcs:
                before = FUPThrottleState.objects.filter(service_connection=svc, active=True).exists()
                res = service.evaluate_service(svc)
                after = FUPThrottleState.objects.filter(service_connection=svc, active=True).exists()
                
                if res: processed += 1
                if not before and after: throttled += 1
        
        return {'processed': processed, 'throttled': throttled}

    def _cleanup_policy_before_delete(self, policy):
        """
        Full cleanup before hard-deleting a policy:
        1. Release all throttled users
        2. Reset any throttled usage windows
        3. Resolve open violations
        4. Deactivate/unlink linked plans
        5. Return a cleanup summary
        """
        released_users = self._release_all_throttles_for_policy(
            policy=policy,
            reason=f'Policy "{policy.name}" deleted by admin.',
        )

        reset_windows = FUPUsageWindow.objects.filter(
            policy=policy,
            is_throttled=True,
        ).update(
            is_throttled=False,
            status='RESET',
        )

        resolved_violations = FUPViolation.objects.filter(
            policy=policy,
            status='OPEN',
        ).update(
            status='RESOLVED',
            notes='Auto-resolved during policy deletion.',
        )

        billing_links_removed = policy.plan_links.filter(is_active=True).update(is_active=False)
        hotspot_links_removed = policy.hotspot_plan_links.filter(is_active=True).update(is_active=False)

        return {
            'users_released': released_users,
            'usage_windows_reset': reset_windows,
            'open_violations_resolved': resolved_violations,
            'billing_links_removed': billing_links_removed,
            'hotspot_links_removed': hotspot_links_removed,
        }

    # --- Standard Methods ---

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        """
        Destructive-but-safe delete:
        - release throttled users back to original plan speed
        - reset throttled usage windows
        - resolve open violations
        - unlink all plans
        - delete the policy
        """
        policy = self.get_object()
        policy_name = policy.name
        policy_id = str(policy.id)

        try:
            with transaction.atomic():
                cleanup_summary = self._cleanup_policy_before_delete(policy)

                # Optional audit attempt before delete.
                # NOTE: because FUPAuditLog.policy is tied to the policy, this log will
                # disappear if it cascades with delete. If you later want durable delete
                # logs, move delete auditing to a global audit model.
                self._log_policy_event(
                    policy=policy,
                    event_type='POLICY_DELETED',
                    message=f'Policy "{policy.name}" deleted with automatic cleanup.',
                    metadata={
                        'action': 'delete',
                        **cleanup_summary,
                    },
                )

                policy.delete()

        except ProtectedError:
            return Response(
                {'error': 'Delete blocked by database protection rules.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as exc:
            return Response(
                {'error': f'Failed to delete policy cleanly: {str(exc)}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response({
            'message': f'Policy "{policy_name}" deleted successfully.',
            'deleted_policy_id': policy_id,
            **cleanup_summary,
        }, status=status.HTTP_200_OK)

    # --- Custom Actions ---

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        policy = self.get_object()
        with transaction.atomic():
            policy.status = 'ACTIVE'
            policy.is_active = True
            policy.updated_by = request.user
            policy.save(update_fields=['status', 'is_active', 'updated_by', 'updated_at'])
        
        result = self._evaluate_all_linked_services_for_policy(policy)
        self._log_policy_event(policy, 'POLICY_UPDATED', f'Policy {policy.name} activated.', metadata=result)
        
        return Response({
            'message': 'Policy activated and users evaluated.',
            'services_processed': result['processed'],
            'users_throttled': result['throttled']
        })

    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        policy = self.get_object()
        with transaction.atomic():
            policy.status = 'INACTIVE'
            policy.is_active = False
            policy.updated_by = request.user
            policy.save(update_fields=['status', 'is_active', 'updated_by', 'updated_at'])
            
        released = self._release_all_throttles_for_policy(policy, 'Policy Deactivated')
        
        # Reset current usage windows for this policy
        FUPUsageWindow.objects.filter(policy=policy, is_throttled=True).update(is_throttled=False, status='RESET')
        
        self._log_policy_event(policy, 'POLICY_UPDATED', f'Policy {policy.name} deactivated.', metadata={'released': released})
        return Response({'message': 'Policy deactivated and users released.', 'users_released': released})

    @action(detail=True, methods=['get'])
    def linked_plans(self, request, pk=None):
        policy = self.get_object()
        billing_links = policy.plan_links.select_related('plan').filter(is_active=True)
        hotspot_links = policy.hotspot_plan_links.select_related('hotspot_plan').filter(is_active=True)
        return Response({
            'billing_plans': [{'id': l.plan.id, 'name': l.plan.name} for l in billing_links],
            'hotspot_plans': [{'id': l.hotspot_plan.id, 'name': l.hotspot_plan.name} for l in hotspot_links]
        })

    @action(detail=True, methods=['get'])
    def available_plans(self, request, pk=None):
        policy = self.get_object()
        linked_billing_ids = set(policy.plan_links.values_list('plan_id', flat=True))
        linked_hotspot_ids = set(policy.hotspot_plan_links.values_list('hotspot_plan_id', flat=True))

        billing_plans = Plan.objects.filter(is_active=True).annotate(
            total_subs=Count('service_connections', distinct=True)
        )
        hotspot_plans = HotspotPlan.objects.filter(is_active=True).select_related('router')

        billing_data = []
        for p in billing_plans:
            billing_data.append({
                'id': p.id,
                'name': p.name,
                'plan_type': p.plan_type,
                'base_price': str(p.base_price),
                'validity_display': p.validity_display,
                'validity_type': p.validity_type,
                'duration_days': p.duration_days,
                'validity_hours': p.validity_hours,
                'validity_minutes': p.validity_minutes,
                'validity_months': p.validity_months,
                'download_speed': p.download_speed,
                'upload_speed': p.upload_speed,
                'subscriber_count': p.total_subs,
                'is_active': p.is_active,
                'already_linked': p.id in linked_billing_ids,
            })

        hotspot_data = []
        for p in hotspot_plans:
            hotspot_data.append({
                'id': str(p.id),
                'name': p.name,
                'plan_type': 'HOTSPOT',
                'base_price': str(p.price),
                'validity_display': p.duration_display,
                'validity_type': p.validity_type,
                'validity_value': p.validity_value,
                'download_speed': p.download_speed,
                'upload_speed': p.upload_speed,
                'speed_unit': p.speed_unit,
                'subscriber_count': p.sessions.filter(status='active').count(),
                'is_active': p.is_active,
                'already_linked': p.id in linked_hotspot_ids,
            })

        return Response({
            'billing_plans': billing_data,
            'hotspot_plans': hotspot_data,
        })

    @action(detail=True, methods=['post'])
    def link_plans(self, request, pk=None):
        policy = self.get_object()
        serializer = LinkPlansSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        plan_ids = serializer.validated_data.get('plan_ids', [])
        hotspot_plan_ids = serializer.validated_data.get('hotspot_plan_ids', [])
        
        # Link billing plans
        for p_id in plan_ids:
            FUPPolicyPlan.objects.update_or_create(
                policy=policy,
                plan_id=p_id,
                defaults={'is_active': True, 'linked_by': request.user}
            )
        
        # Link hotspot plans with audit tracking
        for h_id in hotspot_plan_ids:
            FUPPolicyHotspotPlan.objects.update_or_create(
                policy=policy,
                hotspot_plan_id=h_id,
                defaults={
                    'is_active': True,
                    'linked_by': request.user,
                }
            )
        
        # Audit log the linking operation
        self._log_policy_event(
            policy,
            'PLAN_LINKED',
            f'Plans linked to policy {policy.name}.',
            metadata={
                'plan_ids': plan_ids,
                'hotspot_plan_ids': hotspot_plan_ids,
            }
        )
        
        return Response({'status': 'linked'})

    @action(detail=True, methods=['post'])
    def unlink_plans(self, request, pk=None):
        policy = self.get_object()
        serializer = LinkPlansSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        plan_ids = serializer.validated_data.get('plan_ids', [])
        hotspot_plan_ids = serializer.validated_data.get('hotspot_plan_ids', [])
        
        policy.plan_links.filter(plan_id__in=plan_ids).update(is_active=False)
        policy.hotspot_plan_links.filter(hotspot_plan_id__in=hotspot_plan_ids).update(is_active=False)
        
        # Audit log the unlinking operation
        self._log_policy_event(
            policy,
            'PLAN_UNLINKED',
            f'Plans unlinked from policy {policy.name}.',
            metadata={
                'plan_ids': plan_ids,
                'hotspot_plan_ids': hotspot_plan_ids,
            }
        )
        
        return Response({'status': 'unlinked'})

    @action(detail=True, methods=['post'])
    def run_enforcement(self, request, pk=None):
        policy = self.get_object()
        service = FUPEnforcementService()
        count = 0
        
        for link in policy.plan_links.filter(is_active=True).select_related('plan'):
            svcs = ServiceConnection.objects.filter(status='ACTIVE', plan=link.plan)
            for svc in svcs:
                service.evaluate_service(svc)
                count += 1
        
        # Using POLICY_UPDATED instead of MANUAL_ENFORCEMENT to match EVENT_CHOICES
        self._log_policy_event(
            policy,
            'POLICY_UPDATED',
            f'Manual enforcement run on policy {policy.name}.',
            metadata={'services_processed': count, 'action': 'manual_enforcement'}
        )
                
        return Response({'message': 'Manual enforcement complete.', 'services_processed': count})


class FUPViolationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = FUPViolation.objects.select_related('customer', 'policy', 'plan').all()
    serializer_class = FUPViolationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        policy_id = self.request.query_params.get('policy_id')
        status_val = self.request.query_params.get('status')
        
        if policy_id:
            qs = qs.filter(policy_id=policy_id)
        if status_val:
            qs = qs.filter(status=status_val)
            
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
                item.customer.customer_code,
                item.policy.name,
                round(item.total_usage_bytes / (1024**3), 2),
                item.action_taken,
                item.status,
                item.occurred_at.isoformat()
            ])
            
        return response


class FUPThrottleStateViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = FUPThrottleState.objects.select_related('customer', 'policy').all()
    serializer_class = FUPThrottleStateSerializer
    permission_classes = [IsAuthenticated]


class FUPUsageWindowViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for FUP Usage Windows.
    This provides the data needed for the "Users Under FUP" tab with progress bars.
    
    Query Parameters:
    - policy_id: Filter by policy ID
    - status: Filter by status (NORMAL, VIOLATED, THROTTLED, RESET)
    - throttled: Filter by is_throttled (true/false)
    - search: Search by customer code, name, or policy name
    - current_only: Only show current windows (default: true)
    """
    queryset = FUPUsageWindow.objects.select_related(
        'customer', 'policy', 'plan', 'service_connection'
    ).all()
    serializer_class = FUPUsageWindowSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()

        policy_id = self.request.query_params.get('policy_id')
        status_val = self.request.query_params.get('status')
        throttled = self.request.query_params.get('throttled')
        search = self.request.query_params.get('search')
        current_only = self.request.query_params.get('current_only', 'true')

        if policy_id:
            qs = qs.filter(policy_id=policy_id)

        if status_val:
            qs = qs.filter(status=status_val)

        if throttled in ('true', 'false'):
            qs = qs.filter(is_throttled=(throttled == 'true'))

        if search:
            qs = qs.filter(
                Q(customer__customer_code__icontains=search) |
                Q(customer__user__first_name__icontains=search) |
                Q(customer__user__last_name__icontains=search) |
                Q(policy__name__icontains=search)
            )

        if current_only == 'true':
            now = timezone.now()
            qs = qs.filter(period_start__lte=now, period_end__gt=now)

        # Order by usage percentage descending (most critical first) and then by customer code
        return qs.order_by('-usage_percent', 'customer__customer_code')


class FUPAnalyticsOverviewView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        data = FUPAnalyticsService().overview()
        return Response(FUPAnalyticsOverviewSerializer(data).data)