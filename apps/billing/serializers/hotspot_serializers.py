"""
Hotspot Serializers for Admin API
"""
from rest_framework import serializers
from apps.billing.models.hotspot_models import HotspotPlan, HotspotSession, HotspotBranding


class HotspotPlanSerializer(serializers.ModelSerializer):
    """Serializer for HotspotPlan CRUD operations"""
    
    # Computed display fields
    duration_display = serializers.CharField(read_only=True)
    data_limit_display = serializers.CharField(read_only=True)
    speed_display = serializers.CharField(read_only=True)
    valid_days_list = serializers.ListField(read_only=True)
    total_validity_minutes = serializers.IntegerField(read_only=True)
    
    # Router IDs for multi-router support
    router_ids = serializers.ListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        help_text="List of router IDs where this plan is available"
    )
    
    # Valid days input
    valid_days = serializers.DictField(
        child=serializers.BooleanField(),
        write_only=True,
        required=False,
        help_text="Dict of day names to boolean values"
    )
    
    class Meta:
        model = HotspotPlan
        fields = [
            'id', 'router_id', 'name', 'description', 'price', 'currency',
            # New validity fields
            'validity_type', 'validity_value',
            # Legacy fields for backward compatibility
            'duration_minutes', 'duration_display',
            # New limitation fields  
            'limitation_type', 'data_limit_value', 'data_limit_unit',
            # Legacy fields for backward compatibility
            'data_limit_mb', 'data_limit_display',
            # New speed fields
            'download_speed', 'upload_speed', 'speed_unit', 'speed_display',
            # Legacy speed field
            'speed_limit_mbps',
            # Session limits
            'simultaneous_devices',
            # Valid days - individual fields
            'valid_monday', 'valid_tuesday', 'valid_wednesday', 'valid_thursday',
            'valid_friday', 'valid_saturday', 'valid_sunday', 'valid_days_list',
            # Input fields
            'valid_days', 'router_ids',
            # MikroTik integration
            'mikrotik_profile',
            # Display settings
            'is_active', 'is_popular', 'sort_order',
            # Computed
            'total_validity_minutes',
            # Timestamps
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'router_id', 
            'duration_display', 'data_limit_display', 'speed_display',
            'valid_days_list', 'total_validity_minutes',
            # Legacy fields computed from new fields
            'duration_minutes', 'data_limit_mb', 'speed_limit_mbps',
            'created_at', 'updated_at'
        ]
    
    def create(self, validated_data):
        # Extract multi-value fields
        router_ids = validated_data.pop('router_ids', [])
        valid_days = validated_data.pop('valid_days', {})
        
        # Apply valid days
        if valid_days:
            for day, value in valid_days.items():
                field_name = f'valid_{day.lower()}'
                if hasattr(HotspotPlan, field_name):
                    validated_data[field_name] = value
        
        # Create the plan
        instance = super().create(validated_data)
        
        # Add additional routers
        if router_ids:
            from apps.network.models import Router
            routers = Router.objects.filter(id__in=router_ids)
            instance.routers.set(routers)
        
        return instance
    
    def update(self, instance, validated_data):
        # Extract multi-value fields
        router_ids = validated_data.pop('router_ids', None)
        valid_days = validated_data.pop('valid_days', {})
        
        # Apply valid days
        if valid_days:
            for day, value in valid_days.items():
                field_name = f'valid_{day.lower()}'
                if hasattr(HotspotPlan, field_name):
                    validated_data[field_name] = value
        
        # Update the plan
        instance = super().update(instance, validated_data)
        
        # Update additional routers
        if router_ids is not None:
            from apps.network.models import Router
            routers = Router.objects.filter(id__in=router_ids)
            instance.routers.set(routers)
        
        return instance


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
