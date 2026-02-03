"""
Serializers for ServiceConnection model
"""
from rest_framework import serializers
from apps.customers.models import ServiceConnection
from apps.billing.models import Plan


class ServicePlanNestedSerializer(serializers.ModelSerializer):
    """Minimal plan serializer for nesting in service responses"""
    price = serializers.DecimalField(source='base_price', max_digits=10, decimal_places=2)
    speed_down = serializers.IntegerField(source='download_speed')
    speed_up = serializers.IntegerField(source='upload_speed')
    validity_days = serializers.IntegerField(source='duration_days')
    
    class Meta:
        model = Plan
        fields = [
            'id', 'name', 'description', 'price', 'code', 'plan_type',
            'speed_down', 'speed_up', 'data_limit', 'validity_days',
            'is_active', 'is_popular'
        ]


class ServiceConnectionSerializer(serializers.ModelSerializer):
    """Serializer for service connections"""
    customer_name = serializers.CharField(source='customer.user.get_full_name', read_only=True)
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True)
    plan = ServicePlanNestedSerializer(read_only=True)
    plan_id = serializers.PrimaryKeyRelatedField(
        queryset=Plan.objects.all(),
        source='plan',
        write_only=True,
        required=False,
        allow_null=True
    )
    
    class Meta:
        model = ServiceConnection
        fields = [
            'id', 'customer', 'customer_name', 'customer_code',
            'service_type', 'plan', 'plan_id', 'connection_type', 'auth_connection_type', 'status',
            'ip_address', 'mac_address', 'vlan_id',
            'router_model', 'router_serial', 'ont_model', 'ont_serial',
            'download_speed', 'upload_speed', 'data_cap', 'qos_profile',
            'installation_address', 'installation_notes', 'installed_by',
            'monthly_price', 'setup_fee', 'prorated_billing',
            'auto_renew', 'contract_period',
            'activation_date', 'suspension_date', 'termination_date',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'activation_date', 'suspension_date', 'termination_date',
            'installed_by', 'created_at', 'updated_at'
        ]


class ServiceCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating service connections"""
    
    # Provide defaults for required fields that may not be sent
    download_speed = serializers.IntegerField(default=10, required=False)
    upload_speed = serializers.IntegerField(default=5, required=False)
    monthly_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, default=0, required=False
    )
    
    # Accept both 'plan' and 'plan_id' for flexibility
    plan = serializers.PrimaryKeyRelatedField(
        queryset=Plan.objects.all(),
        required=False,
        allow_null=True
    )
    
    # RADIUS password - if provided, use this as RADIUS password
    # Otherwise auto-generate one
    radius_password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="Password for RADIUS authentication (PPPoE/Hotspot login)"
    )
    
    class Meta:
        model = ServiceConnection
        fields = [
            'service_type', 'plan', 'connection_type', 'auth_connection_type',
            'ip_address', 'mac_address', 'vlan_id',
            'router_model', 'router_serial', 'ont_model', 'ont_serial',
            'download_speed', 'upload_speed', 'data_cap', 'qos_profile',
            'installation_address', 'installation_notes',
            'monthly_price', 'setup_fee', 'prorated_billing',
            'auto_renew', 'contract_period', 'status', 'radius_password'
        ]
    
    def validate_mac_address(self, value):
        if value:
            import re
            mac_pattern = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$')
            if not mac_pattern.match(value):
                raise serializers.ValidationError('Invalid MAC address format')
        return value
    
    def create(self, validated_data):
        """Create service and pass RADIUS password to signal."""
        radius_password = validated_data.pop('radius_password', None)
        instance = super().create(validated_data)
        
        # Attach the password so the signal can use it
        if radius_password:
            instance._radius_password = radius_password
            # Trigger save again to let signal pick it up
            instance.save()
        
        return instance
    
    def to_representation(self, instance):
        """Return the full service with nested plan after creation"""
        return ServiceConnectionSerializer(instance).data


class ServiceActivationSerializer(serializers.ModelSerializer):
    """Serializer for activating services"""
    
    class Meta:
        model = ServiceConnection
        fields = ['status', 'activation_date', 'installed_by']
        read_only_fields = ['activation_date', 'installed_by']


class ServiceSuspensionSerializer(serializers.ModelSerializer):
    """Serializer for suspending services"""
    reason = serializers.CharField(write_only=True, required=False)
    
    class Meta:
        model = ServiceConnection
        fields = ['status', 'suspension_date', 'reason']
        read_only_fields = ['suspension_date']
