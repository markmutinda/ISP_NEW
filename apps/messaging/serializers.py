# apps/messaging/serializers.py
from rest_framework import serializers
from .models import SMSMessage, SMSTemplate, SMSCampaign, SMSGatewayConfig, SMSNotificationSettings, SMSUnitTopup, TenantSMSWallet
from .services.gateway_dispatcher import PROVIDER_FIELDS
from apps.customers.serializers.customer_serializers import CustomerListSerializer


class SMSTemplateSerializer(serializers.ModelSerializer):
    """Serializer for SMS Templates (list, create, retrieve, update, delete)"""

    class Meta:
        model = SMSTemplate
        fields = [
            'id',
            'name',
            'content',
            'variables',
            'usage_count',
            'is_active',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['usage_count', 'created_at', 'updated_at']


class SMSTemplateCreateUpdateSerializer(serializers.ModelSerializer):
    """Used specifically for create & update (to allow partial updates safely)"""

    class Meta:
        model = SMSTemplate
        fields = ['name', 'content', 'variables', 'is_active', 'event_type']
        extra_kwargs = {
            'variables': {'required': False, 'allow_null': True},
            'event_type': {'required': False, 'allow_blank': True},
        }


class SMSCampaignSerializer(serializers.ModelSerializer):
    """Full serializer for campaigns (list, retrieve, stats)"""

    template_name = serializers.CharField(source='template.name', read_only=True, allow_null=True)

    class Meta:
        model = SMSCampaign
        fields = [
            'id',
            'name',
            'message',
            'template',
            'template_name',
            'recipient_filter',
            'recipient_count',
            'delivered_count',
            'failed_count',
            'status',
            'scheduled_at',
            'started_at',
            'completed_at',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'recipient_count', 'delivered_count', 'failed_count',
            'started_at', 'completed_at', 'created_at', 'updated_at'
        ]


class SMSCampaignCreateUpdateSerializer(serializers.ModelSerializer):
    """For create & update actions"""

    class Meta:
        model = SMSCampaign
        fields = [
            'name',
            'message',
            'template',
            'recipient_filter',
            'scheduled_at',
        ]
        extra_kwargs = {
            'template': {'required': False, 'allow_null': True},
            'scheduled_at': {'required': False},
        }


class SMSMessageSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.user.get_full_name', read_only=True, allow_null=True)
    customer = CustomerListSerializer(read_only=True, required=False, allow_null=True)  # ← added
    template_name = serializers.CharField(source='template.name', read_only=True, allow_null=True)
    campaign_name = serializers.CharField(source='campaign.name', read_only=True, allow_null=True)

    class Meta:
        model = SMSMessage
        fields = [
            'id', 'recipient', 'recipient_name', 'customer', 'customer_name',
            'message', 'status', 'type', 'template', 'template_name',
            'campaign', 'campaign_name', 'provider', 'provider_message_id',
            'cost', 'error_message', 'sent_at', 'delivered_at', 'created_at',
        ]
        read_only_fields = [
            'status', 'cost', 'provider_message_id', 'error_message',
            'sent_at', 'delivered_at', 'created_at',
            'customer_name', 'template_name', 'campaign_name',
        ]

class SMSMessageCreateSerializer(serializers.ModelSerializer):
    """For sending single SMS"""

    class Meta:
        model = SMSMessage
        fields = ['recipient', 'recipient_name', 'customer', 'message', 'template']


class SMSBulkCreateSerializer(serializers.Serializer):
    """For bulk SMS sending"""

    recipients = serializers.ListField(
        child=serializers.CharField(max_length=20),
        min_length=1,
        help_text="List of phone numbers (e.g. ['+254712345678', ...])"
    )
    message = serializers.CharField(required=False, allow_blank=True)
    template = serializers.PrimaryKeyRelatedField(
        queryset=SMSTemplate.objects.filter(is_active=True),
        required=False,
        allow_null=True
    )

    def validate(self, data):
        if not data.get('message') and not data.get('template'):
            raise serializers.ValidationError(
                "Either 'message' or 'template' must be provided."
            )
        return data


class SMSCampaignStartSerializer(serializers.Serializer):
    """Used when starting a campaign (can be empty or have optional params)"""
    pass  # can add scheduled_time override etc. later


class SMSStatsSerializer(serializers.Serializer):
    """Response serializer for /sms/stats/"""

    total_sent = serializers.IntegerField()
    delivered = serializers.IntegerField()
    pending = serializers.IntegerField()
    failed = serializers.IntegerField()
    delivery_rate = serializers.FloatField()
    total_cost = serializers.DecimalField(max_digits=10, decimal_places=2)
    messages_today = serializers.IntegerField()
    messages_this_week = serializers.IntegerField()


# ============================================================
# FIXED SMSBalanceSerializer - handles None, dict, and large numbers
# ============================================================
class SMSBalanceSerializer(serializers.Serializer):
    """Response serializer for /sms/balance/"""
    
    # FIX: Changed from FloatField to DecimalField with allow_null=True
    # This handles None values and large numbers without precision loss
    balance = serializers.DecimalField(
        max_digits=20, 
        decimal_places=4, 
        allow_null=True, 
        default=0
    )
    currency = serializers.CharField(default='KES')
    unit_cost = serializers.DecimalField(max_digits=10, decimal_places=4, default=0.50)
    units_remaining = serializers.IntegerField(default=0)
    provider = serializers.CharField(default='unknown')
    last_updated = serializers.DateTimeField(required=False, allow_null=True)

    def to_representation(self, instance):
        """
        Override to ensure balance is always a number, not a dict,
        and handle None values gracefully.
        """
        # Create a copy to avoid mutating the original
        data = super().to_representation(instance)
        
        # Handle case where balance might be a dict from some providers
        balance_value = instance.get('balance') if isinstance(instance, dict) else data.get('balance')
        
        if isinstance(balance_value, dict):
            # Extract balance from dict if needed
            data['balance'] = balance_value.get('balance', 0)
        elif balance_value is None:
            data['balance'] = 0
        elif isinstance(balance_value, (int, float)):
            # Convert to Decimal-friendly format
            data['balance'] = float(balance_value)
        
        # Ensure units_remaining is an integer
        if data.get('units_remaining') is None:
            data['units_remaining'] = 0
        
        return data


class SMSRetrySerializer(serializers.Serializer):
    """Optional: can be empty or add reason/note later"""
    pass


class SMSGatewayConfigSerializer(serializers.ModelSerializer):
    """Full serializer – masks secrets on read."""
    provider_display = serializers.CharField(source='get_provider_display', read_only=True)
    field_labels = serializers.SerializerMethodField()

    class Meta:
        model = SMSGatewayConfig
        fields = [
            'id', 'provider', 'provider_display', 'is_active', 'use_inbuilt_system',
            'api_key', 'api_secret', 'username', 'sender_id',
            'extra_config',
            'auto_payment_confirmation', 'auto_expiry_reminder',
            'auto_welcome_message', 'auto_service_suspension',
            'field_labels',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']

    def get_field_labels(self, obj):
        if obj.use_inbuilt_system:
            return {}  # No fields needed for inbuilt
        return PROVIDER_FIELDS.get(obj.provider, {})

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Don't show masked keys for inbuilt system (there are none)
        if instance.use_inbuilt_system:
            data['api_key'] = ''
            data['api_secret'] = ''
            return data
        # Mask secrets for custom providers
        for field in ('api_key', 'api_secret'):
            val = data.get(field) or ''
            if len(val) > 4:
                data[field] = '•' * (len(val) - 4) + val[-4:]
        return data


class SMSGatewayConfigWriteSerializer(serializers.ModelSerializer):
    """Write serializer – accepts raw credentials."""

    class Meta:
        model = SMSGatewayConfig
        fields = [
            'provider', 'is_active', 'use_inbuilt_system',
            'api_key', 'api_secret', 'username', 'sender_id',
            'extra_config',
            'auto_payment_confirmation', 'auto_expiry_reminder',
            'auto_welcome_message', 'auto_service_suspension',
        ]

    def validate(self, attrs):
        use_inbuilt = attrs.get(
            'use_inbuilt_system',
            self.instance.use_inbuilt_system if self.instance else False
        )

        if not use_inbuilt:
            # Only require api_key for custom providers on first creation
            if not self.instance and not attrs.get('api_key'):
                raise serializers.ValidationError({
                    'api_key': 'API key is required when not using the inbuilt system.'
                })

        # Force inbuilt to use bytewave provider internally
        if use_inbuilt:
            attrs['provider'] = 'bytewave'
            attrs['api_key'] = attrs.get('api_key', '')
            attrs['api_secret'] = attrs.get('api_secret', '')

        # Activate and deactivate others
        attrs['is_active'] = True
        SMSGatewayConfig.objects.exclude(
            pk=self.instance.pk if self.instance else None
        ).update(is_active=False)

        return attrs


# ============================================================
# NEW SERIALIZERS ADDED BELOW
# ============================================================

class SMSNotificationSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = SMSNotificationSettings
        fields = [
            'use_inbuilt_system',
            # hotspot
            'hotspot_new_subscription', 'hotspot_welcome',
            'hotspot_session_expiry', 'hotspot_expiry_minutes_before',
            'hotspot_payment_failed', 'hotspot_session_expired',
            # pppoe — reduced set (removed deprecated fields)
            'pppoe_welcome',
            'pppoe_payment_confirmation',  # MERGED: handles both payment AND renewal confirmations
            'pppoe_expiry_reminder',
            'pppoe_expiry_intervals',
            'pppoe_expiry_notification',  # NEW: one-time expiry notification
            # system notifications
            'system_router_offline',
            'system_alert_phone',
            # router offline/online alert configuration (JSON array & toggle)
            'router_offline_numbers',
            'router_offline_enabled',
            'updated_at',
        ]
        read_only_fields = ['updated_at']


class SMSUnitTopupSerializer(serializers.ModelSerializer):
    class Meta:
        model = SMSUnitTopup
        fields = [
            'id', 'units_purchased', 'amount_paid',
            'payment_reference', 'payment_method',
            'status', 'checkout_request_id', 'notes',
            'created_at',
        ]
        read_only_fields = ['id', 'status', 'created_at']


class SMSWalletSerializer(serializers.Serializer):
    """Combines wallet balance + topup history."""
    sms_units = serializers.DecimalField(max_digits=14, decimal_places=2)
    sell_price_per_unit = serializers.DecimalField(max_digits=10, decimal_places=4)
    topup_history = SMSUnitTopupSerializer(many=True)