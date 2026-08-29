# apps/network/serializers/access_point_serializers.py
from django.utils import timezone
from rest_framework import serializers
from apps.network.models.access_point_models import AccessPoint


class AccessPointSerializer(serializers.ModelSerializer):
    router_name = serializers.CharField(source='router.name', read_only=True)
    seconds_since_seen = serializers.SerializerMethodField()

    class Meta:
        model = AccessPoint
        fields = [
            'id', 'router', 'router_name', 'parent', 'name',
            'mac_address', 'ip_address', 'pos_x', 'pos_y',
            'status', 'last_seen', 'last_checked', 'seconds_since_seen',
            'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['status', 'last_seen', 'last_checked']

    def get_seconds_since_seen(self, obj):
        if not obj.last_seen:
            return None
        return int((timezone.now() - obj.last_seen).total_seconds())

    def validate_mac_address(self, value):
        v = value.strip().upper().replace('-', ':')
        if len(v) != 17:
            raise serializers.ValidationError("Enter a valid MAC address (AA:BB:CC:DD:EE:FF)")
        return v