from rest_framework import serializers
from apps.network.models.ip_binding_models import IPBinding


class IPBindingSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    router_name = serializers.CharField(source='router.name', read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    time_remaining_minutes = serializers.IntegerField(read_only=True)

    class Meta:
        model = IPBinding
        fields = [
            'id', 'router', 'router_name', 'plan', 'plan_name', 'name',
            'mac_address', 'ip_address', 'status', 'activated_at', 'expires_at',
            'is_active', 'time_remaining_minutes', 'notes', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'status', 'activated_at', 'created_at', 'updated_at']

    def validate_mac_address(self, value):
        v = value.strip().upper().replace('-', ':')
        if len(v) != 17:
            raise serializers.ValidationError("Enter a valid MAC address (AA:BB:CC:DD:EE:FF)")
        return v