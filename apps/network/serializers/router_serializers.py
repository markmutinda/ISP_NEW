# apps/network/serializers/router_serializers.py

from rest_framework import serializers
from apps.network.models.router_models import Router, RouterEvent


class RouterEventSerializer(serializers.ModelSerializer):
    """Serializer for RouterEvent model"""
    event_type_display = serializers.CharField(source='get_event_type_display', read_only=True)
    router_name = serializers.CharField(source='router.name', read_only=True)

    class Meta:
        model = RouterEvent
        fields = [
            'id',
            'router',
            'router_name',
            'event_type',
            'event_type_display',
            'message',
            'created_at',
        ]
        read_only_fields = ['created_at']


class RouterSerializer(serializers.ModelSerializer):
    """
    Main serializer for Router model.
    Includes helpful read-only fields for frontend display.
    """
    company_name = serializers.CharField(source='company.name', read_only=True)
    router_type_display = serializers.CharField(source='get_router_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    # Optional: Show auth status in a friendly way
    auth_status = serializers.SerializerMethodField()

    class Meta:
        model = Router
        fields = [
            'id',
            'company',
            'company_name',
            'name',
            'ip_address',
            'mac_address',
            'api_port',
            'api_username',
            'api_password',
            'router_type',
            'router_type_display',
            'model',
            'firmware_version',
            'location',
            'latitude',
            'longitude',
            'status',
            'status_display',
            'total_users',
            'active_users',
            'uptime',
            'uptime_percentage',
            'sla_target',
            'last_seen',
            'tags',
            'notes',
            'is_active',
            'auth_key',
            'is_authenticated',
            'authenticated_at',
            'auth_status',
            'created_at',
            'updated_at',
        ]
        extra_kwargs = {
            'api_password': {'write_only': True},  # Never return password
            'auth_key': {'read_only': True},       # Only shown via dedicated endpoint if needed
        }

    def get_auth_status(self, obj):
        if obj.is_authenticated:
            return "Authenticated"
        elif obj.auth_key:
            return "Pending Authentication"
        return "Not Configured"

    def validate(self, data):
        """
        Additional validation if needed (e.g., IP uniqueness per company)
        """
        if 'ip_address' in data and data['ip_address']:
            existing = Router.objects.filter(
                ip_address=data['ip_address'],
                company=self.instance.company if self.instance else data.get('company')
            )
            if self.instance:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise serializers.ValidationError("A router with this IP address already exists in your company.")

        return data       