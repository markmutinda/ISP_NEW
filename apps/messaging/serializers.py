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
        fields = ['name', 'content', 'variables', 'is_active']
        extra_kwargs = {
            'variables': {'required': False, 'allow_null': True},
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


class SMSBalanceSerializer(serializers.Serializer):
    """Response serializer for /sms/balance/"""

    balance = serializers.FloatField()
    currency = serializers.CharField(default='KES')
    unit_cost = serializers.DecimalField(max_digits=5, decimal_places=2)
    units_remaining = serializers.IntegerField()
    provider = serializers.CharField(default='africastalking')
    last_updated = serializers.DateTimeField()


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
        return PROVIDER_FIELDS.get(obj.provider, {})

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Mask secrets: show only last 4 chars
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
        # If activating, deactivate all others
        if attrs.get('is_active'):
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
            # pppoe
            'pppoe_welcome', 'pppoe_payment_confirmation',
            'pppoe_expiry_reminder', 'pppoe_expiry_days_before',
            'pppoe_service_suspended', 'pppoe_service_resumed',
            'pppoe_plan_changed', 'pppoe_renewal_confirmation',
            'pppoe_new_subscription',
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