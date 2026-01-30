"""
Serializers for ServiceConnection model
"""
from rest_framework import serializers
from apps.customers.models import ServiceConnection


class ServiceConnectionSerializer(serializers.ModelSerializer):
    """Serializer for service connections"""
    customer_name = serializers.CharField(source='customer.user.get_full_name', read_only=True)
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True)
    
    class Meta:
        model = ServiceConnection
        fields = [
            'id', 'customer', 'customer_name', 'customer_code',
            'service_type', 'plan', 'connection_type', 'auth_connection_type', 'status',
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
    
    class Meta:
        model = ServiceConnection
        fields = [
            'service_type', 'plan', 'connection_type', 'auth_connection_type',
            'ip_address', 'mac_address', 'vlan_id',
            'router_model', 'router_serial', 'ont_model', 'ont_serial',
            'download_speed', 'upload_speed', 'data_cap', 'qos_profile',
            'installation_address', 'installation_notes',
            'monthly_price', 'setup_fee', 'prorated_billing',
            'auto_renew', 'contract_period', 'status'
        ]
    
    def validate_mac_address(self, value):
        if value:
            import re
            mac_pattern = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$')
            if not mac_pattern.match(value):
                raise serializers.ValidationError('Invalid MAC address format')
        return value


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
