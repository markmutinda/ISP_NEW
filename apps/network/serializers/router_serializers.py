# apps/network/serializers/router_serializers.py

from rest_framework import serializers
from apps.network.models.router_models import Router, RouterEvent
from apps.core.models import Company


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
    company_name = serializers.CharField(source='company.name', read_only=True)
    router_type_display = serializers.CharField(source='get_router_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    auth_status = serializers.SerializerMethodField()
    is_editable = serializers.SerializerMethodField()

    class Meta:
        model = Router
        fields = [
            'id', 'company', 'company_name', 'name', 'ip_address', 'mac_address',
            'api_port', 'api_username', 'api_password', 'router_type', 'router_type_display',
            'model', 'firmware_version', 'location', 'latitude', 'longitude',
            'status', 'status_display', 'total_users', 'active_users', 'uptime',
            'uptime_percentage', 'sla_target', 'last_seen', 'tags', 'notes',
            'is_active', 'auth_key', 'is_authenticated', 'authenticated_at',
            'auth_status', 'shared_secret', 'is_editable', 'created_at', 'updated_at',
        ]
        extra_kwargs = {
            'api_password': {'write_only': True, 'required': False, 'allow_blank': True},
            'auth_key': {'read_only': True},
            'company': {'required': False},
            'ip_address': {'required': False, 'allow_blank': True, 'allow_null': True},
            'mac_address': {'required': False, 'allow_blank': True, 'allow_null': True},
            'api_username': {'required': False, 'allow_blank': True, 'allow_null': True},
            'api_port': {'required': False},
            'model': {'required': False, 'allow_blank': True, 'allow_null': True},
            'firmware_version': {'required': False, 'allow_blank': True, 'allow_null': True},
            'location': {'required': False, 'allow_blank': True, 'allow_null': True},
            'latitude': {'required': False, 'allow_null': True},
            'longitude': {'required': False, 'allow_null': True},
            'uptime': {'required': False, 'allow_blank': True, 'allow_null': True},
            'notes': {'required': False, 'allow_blank': True, 'allow_null': True},
            'tags': {'required': False},
            'status': {'read_only': True},
            'total_users': {'read_only': True},
            'active_users': {'read_only': True},
            'last_seen': {'read_only': True},
            'is_authenticated': {'read_only': True},
            'authenticated_at': {'read_only': True},
            'shared_secret': {'read_only': True},
        }

    def get_auth_status(self, obj):
        if obj.is_authenticated:
            return "Authenticated"
        elif obj.auth_key:
            return "Pending Authentication"
        return "Not Configured"
    
    def get_is_editable(self, obj):
        request = self.context.get('request')
        if request and request.user:
            if request.user.is_superuser:
                return True
            if hasattr(request.user, 'company') and request.user.company and obj.company:
                return request.user.company.id == obj.company.id
        return False

    def create(self, validated_data):
        request = self.context.get('request')
        if request and request.user:
            # If user has a company, assign it
            if hasattr(request.user, 'company') and request.user.company:
                validated_data['company'] = request.user.company
            # If superuser and company not specified, use first company
            elif request.user.is_superuser and 'company' not in validated_data:
                company = Company.objects.first()
                if company:
                    validated_data['company'] = company
        return super().create(validated_data)

    def validate(self, data):
        """
        Validate router data
        """
        # Check for duplicate names within the same company
        if 'name' in data and 'company' in data:
            queryset = Router.objects.filter(name=data['name'], company=data['company'])
            if self.instance:
                queryset = queryset.exclude(pk=self.instance.pk)
            if queryset.exists():
                raise serializers.ValidationError(
                    {"name": "A router with this name already exists in this company."}
                )
        
        # Check for duplicate IP addresses (optional)
        if 'ip_address' in data and data.get('ip_address'):
            queryset = Router.objects.filter(ip_address=data['ip_address'])
            if self.instance:
                queryset = queryset.exclude(pk=self.instance.pk)
            if queryset.exists():
                raise serializers.ValidationError(
                    {"ip_address": "A router with this IP address already exists."}
                )
        
        return data