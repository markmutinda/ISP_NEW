"""
Hotspot Serializers for Admin API
"""
from rest_framework import serializers
from apps.billing.models.hotspot_models import HotspotPlan, HotspotSession, HotspotBranding


class HotspotPlanSerializer(serializers.ModelSerializer):
    """Serializer for HotspotPlan CRUD operations"""
    
    duration_display = serializers.CharField(read_only=True)
    data_limit_display = serializers.CharField(read_only=True)
    
    class Meta:
        model = HotspotPlan
        fields = [
            'id', 'router_id', 'name', 'description', 'price', 'currency',
            'duration_minutes', 'duration_display',
            'data_limit_mb', 'data_limit_display',
            'speed_limit_mbps', 'mikrotik_profile',
            'is_active', 'is_popular', 'sort_order',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'router_id', 'duration_display', 'data_limit_display', 'created_at', 'updated_at']


class HotspotSessionSerializer(serializers.ModelSerializer):
    """Serializer for HotspotSession read operations"""
    
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    plan_price = serializers.DecimalField(source='plan.price', max_digits=10, decimal_places=2, read_only=True)
    time_remaining_minutes = serializers.IntegerField(read_only=True)
    data_remaining_mb = serializers.IntegerField(read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = HotspotSession
        fields = [
            'id', 'session_id', 'router_id',
            'plan_id', 'plan_name', 'plan_price',
            'phone_number', 'mac_address',
            'amount', 'mpesa_receipt',
            'access_code', 'status',
            'activated_at', 'expires_at',
            'data_used_mb', 'time_remaining_minutes', 'data_remaining_mb',
            'is_active', 'failure_reason',
            'created_at', 'updated_at'
        ]
        read_only_fields = fields  # Sessions are read-only for admin


class HotspotBrandingSerializer(serializers.ModelSerializer):
    """Serializer for HotspotBranding CRUD operations"""
    
    logo_url = serializers.SerializerMethodField()
    background_image_url = serializers.SerializerMethodField()
    
    class Meta:
        model = HotspotBranding
        fields = [
            'id', 'router_id',
            'company_name', 'logo', 'logo_url', 
            'background_image', 'background_image_url',
            'primary_color', 'secondary_color', 'text_color', 'background_color',
            'welcome_title', 'welcome_message', 'terms_and_conditions',
            'support_phone', 'support_email',
            'facebook_url', 'twitter_url', 'instagram_url', 'website_url',
            'is_default', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'router_id', 'logo_url', 'background_image_url', 'created_at', 'updated_at']
    
    def get_logo_url(self, obj):
        if obj.logo:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.logo.url)
            return obj.logo.url
        return None
    
    def get_background_image_url(self, obj):
        if obj.background_image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(obj.background_image.url)
            return obj.background_image.url
        return None
