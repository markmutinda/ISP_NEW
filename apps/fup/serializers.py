from rest_framework import serializers
from django.utils import timezone

from .models import (
    FUPPolicy,
    FUPPolicyPlan,
    FUPUsageWindow,
    FUPViolation,
    FUPThrottleState,
)


class FUPPolicySerializer(serializers.ModelSerializer):
    linked_plans_count = serializers.SerializerMethodField()
    users_count = serializers.SerializerMethodField()
    active_violations_count = serializers.SerializerMethodField()
    currently_throttled_count = serializers.SerializerMethodField()

    class Meta:
        model = FUPPolicy
        fields = [
            'id', 'name', 'description',
            'data_limit_gb', 'throttle_download_mbps', 'throttle_upload_mbps',
            'reset_period', 'peak_hour_start', 'peak_hour_end',
            'status', 'auto_enforce', 'notify_on_violation', 'is_active',
            'linked_plans_count', 'users_count',
            'active_violations_count', 'currently_throttled_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']

    def validate(self, data):
        if data.get('data_limit_gb') is not None and data['data_limit_gb'] <= 0:
            raise serializers.ValidationError('Data limit must be greater than 0.')
        if data.get('throttle_download_mbps', 0) <= 0:
            raise serializers.ValidationError('Throttle download speed must be greater than 0.')
        if data.get('throttle_upload_mbps', 0) <= 0:
            raise serializers.ValidationError('Throttle upload speed must be greater than 0.')
        if data.get('reset_period') == 'PEAK_HOURS':
            if not data.get('peak_hour_start') or not data.get('peak_hour_end'):
                raise serializers.ValidationError(
                    'peak_hour_start and peak_hour_end are required for PEAK_HOURS reset period.'
                )
        return data

    def get_linked_plans_count(self, obj):
        # FIX: Count BOTH billing plan links and hotspot plan links
        billing = obj.plan_links.filter(is_active=True).count()
        hotspot = obj.hotspot_plan_links.filter(is_active=True).count()
        return billing + hotspot

    def get_users_count(self, obj):
        pppoe = (
            obj.plan_links
            .filter(is_active=True, plan__service_connections__status='ACTIVE')
            .values('plan__service_connections')
            .distinct()
            .count()
        )
        hotspot = (
            obj.hotspot_plan_links
            .filter(
                is_active=True,
                hotspot_plan__sessions__status='active',
                hotspot_plan__sessions__expires_at__gt=timezone.now(),
            )
            .values('hotspot_plan__sessions')
            .distinct()
            .count()
        )
        return pppoe + hotspot

    def get_active_violations_count(self, obj):
        return obj.violations.filter(status='OPEN').count()

    def get_currently_throttled_count(self, obj):
        return obj.throttle_states.filter(active=True).count()


class FUPPolicyPlanSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    plan_type = serializers.CharField(source='plan.plan_type', read_only=True)
    subscriber_count = serializers.SerializerMethodField()

    class Meta:
        model = FUPPolicyPlan
        fields = ['id', 'policy', 'plan', 'plan_name', 'plan_type', 'subscriber_count', 'is_active', 'linked_at']
        read_only_fields = ['linked_at']

    def get_subscriber_count(self, obj):
        return obj.plan.service_connections.filter(status='ACTIVE').count()


class AvailablePlanSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    plan_type = serializers.CharField()
    subscriber_count = serializers.IntegerField()
    already_linked = serializers.BooleanField()


class FUPUsageWindowSerializer(serializers.ModelSerializer):
    # FIX: Use method fields to handle nullable customer/plan (hotspot sessions have neither)
    customer_name = serializers.SerializerMethodField()
    customer_code = serializers.SerializerMethodField()
    plan_name = serializers.SerializerMethodField()
    policy_name = serializers.CharField(source='policy.name', read_only=True)
    total_gb = serializers.SerializerMethodField()

    class Meta:
        model = FUPUsageWindow
        fields = [
            'id', 'policy', 'policy_name',
            'plan', 'plan_name',
            'service_connection', 'customer', 'customer_name', 'customer_code',
            'period_start', 'period_end',
            'download_bytes', 'upload_bytes', 'total_bytes', 'total_gb',
            'limit_bytes', 'usage_percent', 'status',
            'first_exceeded_at', 'is_throttled', 'throttled_at', 'unthrottled_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']

    def get_customer_name(self, obj):
        if obj.customer:
            try:
                return obj.customer.user.get_full_name() or obj.customer.customer_code
            except Exception:
                return obj.customer.customer_code
        if obj.hotspot_session:
            code = obj.hotspot_session.access_code or ''
            phone = obj.hotspot_session.phone_number or ''
            return f"Hotspot ({code or phone})"
        return "Unknown"

    def get_customer_code(self, obj):
        if obj.customer:
            return obj.customer.customer_code
        if obj.hotspot_session:
            return obj.hotspot_session.access_code or 'HOTSPOT'
        return "N/A"

    def get_plan_name(self, obj):
        if obj.plan:
            return obj.plan.name
        if obj.hotspot_plan:
            return f"{obj.hotspot_plan.name} (Hotspot)"
        if obj.hotspot_session and obj.hotspot_session.plan:
            return f"{obj.hotspot_session.plan.name} (Hotspot)"
        return "Unknown Plan"

    def get_total_gb(self, obj):
        return obj.total_gb


class FUPViolationSerializer(serializers.ModelSerializer):
    # FIX: Use method fields to handle nullable customer/plan
    customer_name = serializers.SerializerMethodField()
    customer_code = serializers.SerializerMethodField()
    policy_name = serializers.CharField(source='policy.name', read_only=True)
    plan_name = serializers.SerializerMethodField()
    usage_gb = serializers.SerializerMethodField()
    limit_gb = serializers.SerializerMethodField()

    class Meta:
        model = FUPViolation
        fields = [
            'id', 'customer', 'customer_name', 'customer_code',
            'service_connection', 'policy', 'policy_name',
            'plan', 'plan_name', 'usage_gb', 'limit_gb',
            'action_taken', 'status', 'notes', 'occurred_at',
        ]

    def get_customer_name(self, obj):
        if obj.customer:
            try:
                return obj.customer.user.get_full_name()
            except Exception:
                return obj.customer.customer_code
        if obj.hotspot_session:
            return f"Hotspot ({obj.hotspot_session.access_code or obj.hotspot_session.phone_number})"
        return "Unknown"

    def get_customer_code(self, obj):
        if obj.customer:
            return obj.customer.customer_code
        if obj.hotspot_session:
            return obj.hotspot_session.access_code or 'HOTSPOT'
        return "N/A"

    def get_plan_name(self, obj):
        if obj.plan:
            return obj.plan.name
        if obj.hotspot_plan:
            return f"{obj.hotspot_plan.name} (Hotspot)"
        if obj.hotspot_session and obj.hotspot_session.plan:
            return f"{obj.hotspot_session.plan.name} (Hotspot)"
        return "Unknown"

    def get_usage_gb(self, obj):
        return round(obj.total_usage_bytes / (1024 ** 3), 2)

    def get_limit_gb(self, obj):
        return round(obj.limit_bytes / (1024 ** 3), 2)


class FUPThrottleStateSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()
    customer_code = serializers.SerializerMethodField()
    policy_name = serializers.CharField(source='policy.name', read_only=True)

    class Meta:
        model = FUPThrottleState
        fields = [
            'id', 'policy', 'policy_name',
            'service_connection', 'customer', 'customer_name', 'customer_code',
            'original_download_mbps', 'original_upload_mbps',
            'throttled_download_mbps', 'throttled_upload_mbps',
            'active', 'reason', 'applied_at', 'released_at', 'last_synced_at',
        ]

    def get_customer_name(self, obj):
        if obj.customer:
            try:
                return obj.customer.user.get_full_name()
            except Exception:
                return obj.customer.customer_code
        if obj.hotspot_session:
            return f"Hotspot ({obj.hotspot_session.access_code})"
        return "Unknown"

    def get_customer_code(self, obj):
        if obj.customer:
            return obj.customer.customer_code
        if obj.hotspot_session:
            return obj.hotspot_session.access_code or 'HOTSPOT'
        return "N/A"


class FUPDashboardSummarySerializer(serializers.Serializer):
    active_policies = serializers.IntegerField()
    users_under_fup = serializers.IntegerField()
    active_violations = serializers.IntegerField()
    currently_throttled = serializers.IntegerField()


class FUPAnalyticsOverviewSerializer(serializers.Serializer):
    violation_trends = serializers.ListField()
    top_violators_this_month = serializers.ListField()
    policy_distribution = serializers.ListField()


class LinkPlansSerializer(serializers.Serializer):
    plan_ids = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    hotspot_plan_ids = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)

    def validate(self, data):
        if not data.get('plan_ids') and not data.get('hotspot_plan_ids'):
            raise serializers.ValidationError("Provide at least one plan_id or hotspot_plan_id.")
        return data