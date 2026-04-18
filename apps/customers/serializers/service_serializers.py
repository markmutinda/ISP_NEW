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
            # ↓ These three lines are the fix for Issue 2
            'billing_account_number',
            'mpesa_account_number',
            'paybill_account_number',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'activation_date', 'suspension_date', 'termination_date',
            'installed_by', 'created_at', 'updated_at',
            'billing_account_number',   # auto-generated, never set by callers
            'mpesa_account_number',      # auto-generated, never set by callers
            'paybill_account_number',    # auto-generated, never set by callers
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
    
    # Router (NAS) assignment — passed through to CustomerRadiusCredentials
    router = serializers.IntegerField(
        write_only=True,
        required=False,
        allow_null=True,
        help_text="Network Router ID for RADIUS NAS assignment"
    )
    
    # IP Pool name — stored as Framed-Pool RADIUS reply attribute
    ip_pool = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        help_text="IP Pool name (Framed-Pool) for the router to assign IPs from"
    )
    
    # Cloud-Led: Specific static IP address assignment (IPAddress PK)
    assigned_ip = serializers.IntegerField(
        write_only=True,
        required=False,
        allow_null=True,
        help_text="IPAddress ID for Cloud-Led static IP assignment (Framed-IP-Address)"
    )
    
    # P4: "Activate Later" — when False, creates service as PENDING
    # and does NOT start the expiration timer or sync to RADIUS
    activate_now = serializers.BooleanField(
        default=True,
        required=False,
        write_only=True,
        help_text=(
            "If True (default), service goes ACTIVE immediately and "
            "expiration timer starts. If False, service is PENDING — "
            "use the /activate/ endpoint later to start the timer."
        )
    )
    activation_delay_minutes = serializers.IntegerField(
        default=0,
        required=False,
        write_only=True,
        help_text=(
            "When activate_now=True and this is > 0, the service is created "
            "as PENDING and auto-activated after the specified minutes (e.g. 60 "
            "for a 1-hour testing window). The expiration timer starts only "
            "after the delay."
        )
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
            'auto_renew', 'contract_period', 'status',
            'radius_password', 'router', 'ip_pool', 'assigned_ip', 'activate_now',
            'activation_delay_minutes',
        ]
    
    def validate_mac_address(self, value):
        if value:
            import re
            mac_pattern = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$')
            if not mac_pattern.match(value):
                raise serializers.ValidationError('Invalid MAC address format')
        return value
    
    def create(self, validated_data):
        """
        Create service connection.
        
        If activate_now=False (Activate Later), the service is created
        as PENDING with no RADIUS sync. The expiration timer does NOT start.
        
        If activate_now=True, we set _radius_password on the model instance
        and trigger a save so the post_save signal creates RADIUS credentials.
        """
        radius_password = validated_data.pop('radius_password', None)
        activate_now = validated_data.pop('activate_now', True)
        activation_delay_minutes = validated_data.pop('activation_delay_minutes', 0)
        radius_router_id = validated_data.pop('router', None)
        radius_ip_pool = validated_data.pop('ip_pool', None)
        assigned_ip_id = validated_data.pop('assigned_ip', None)
        
        # Delayed activation: create as PENDING, schedule auto-activation
        if activate_now and activation_delay_minutes > 0:
            activate_now = False
            validated_data['status'] = 'PENDING'
        
        # If activate_now is False, force status to PENDING
        if not activate_now:
            validated_data['status'] = 'PENDING'
        
        instance = super().create(validated_data)
        
        # The first save (from super().create) triggers post_save with created=True,
        # but _radius_password isn't set yet. The signal will try to auto-generate
        # a password. However, if we want the user-provided password, we need
        # to update the credentials after creation OR trigger a second save.
        
        # Stash router, ip_pool, and assigned_ip for the RADIUS signal to pick up
        if radius_router_id is not None:
            instance._radius_router_id = radius_router_id
        if radius_ip_pool:
            instance._radius_ip_pool = radius_ip_pool
        if assigned_ip_id is not None:
            instance._radius_assigned_ip_id = assigned_ip_id
        
        if activate_now and radius_password:
            # Attach password and trigger save so the signal can pick it up.
            # The signal handles both created=True (first save) and the case
            # where credentials need to be created for an existing service.
            instance._radius_password = radius_password
            instance._force_radius_creation = True  # Signal flag to force creation
            instance.save()
        elif radius_router_id is not None or radius_ip_pool or assigned_ip_id is not None:
            # Even without activate_now, if router/pool/assigned_ip were specified,
            # trigger a save so the signal can update existing credentials
            instance._force_radius_creation = True
            instance.save()
        
        # Schedule delayed activation via Celery if requested
        if activation_delay_minutes > 0:
            from apps.customers.tasks import delayed_service_activation
            delayed_service_activation.apply_async(
                args=[instance.id],
                countdown=activation_delay_minutes * 60,
            )
        
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