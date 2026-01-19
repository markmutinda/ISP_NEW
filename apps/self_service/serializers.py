from rest_framework import serializers
from .models import CustomerSession, ServiceRequest, UsageAlert


class CustomerSessionSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    duration = serializers.SerializerMethodField()
    
    class Meta:
        model = CustomerSession
        fields = [
            'id', 'customer', 'customer_name', 'session_key',
            'ip_address', 'user_agent', 'login_time',
            'logout_time', 'last_activity', 'is_active', 'duration'
        ]
        read_only_fields = fields
    
    def get_duration(self, obj):
        if obj.logout_time:
            duration = obj.logout_time - obj.login_time
        else:
            duration = timezone.now() - obj.login_time
        return str(duration)


class ServiceRequestSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    current_plan_name = serializers.CharField(source='current_plan.name', read_only=True, allow_null=True)
    requested_plan_name = serializers.CharField(source='requested_plan.name', read_only=True, allow_null=True)
    assigned_to_name = serializers.CharField(source='assigned_to.get_full_name', read_only=True, allow_null=True)
    
    class Meta:
        model = ServiceRequest
        fields = [
            'id', 'customer', 'customer_name', 'request_type', 'subject',
            'description', 'status', 'priority', 'current_plan',
            'current_plan_name', 'requested_plan', 'requested_plan_name',
            'current_location', 'requested_location', 'assigned_to',
            'assigned_to_name', 'estimated_completion', 'actual_completion',
            'customer_notes', 'staff_notes', 'created_at', 'updated_at'
        ]
        read_only_fields = ['customer', 'status', 'assigned_to', 'actual_completion', 'staff_notes']


class UsageAlertSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    customer_email = serializers.CharField(source='customer.email', read_only=True)
    
    class Meta:
        model = UsageAlert
        fields = [
            'id', 'customer', 'customer_name', 'customer_email',
            'alert_type', 'trigger_type', 'threshold_value',
            'current_value', 'message', 'is_read', 'is_resolved',
            'triggered_at', 'resolved_at'
        ]
        read_only_fields = fields


class CustomerDashboardSerializer(serializers.Serializer):
    # This is a non-model serializer for dashboard data
    customer = serializers.DictField()
    usage = serializers.DictField()
    billing = serializers.DictField()
    recent_activity = serializers.DictField()
    alerts = UsageAlertSerializer(many=True)
    quick_actions = serializers.ListField()
