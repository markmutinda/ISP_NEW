from rest_framework import serializers
from .models import (
    LoyaltySettings,
    LoyaltyTier,
    LoyaltyMember,
    LoyaltyReward,
    PointsTransaction,
    PointsRule,
)


class LoyaltySettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = LoyaltySettings
        fields = [
            'points_per_currency', 'currency_unit', 'signup_bonus',
            'referral_bonus', 'tenure_monthly_bonus',
            'points_expiry_enabled', 'points_expiry_months', 'expiry_warning_days',
            'notify_points_earned', 'notify_redemption', 'notify_tier_upgrade',
            'notify_monthly_summary', 'program_active', 'auto_enroll_new_customers',
        ]


class LoyaltyTierSerializer(serializers.ModelSerializer):
    members_count = serializers.SerializerMethodField()

    class Meta:
        model = LoyaltyTier
        fields = [
            'id', 'name', 'level', 'min_points', 'max_points',
            'points_multiplier', 'benefits', 'color', 'members_count',
        ]

    def get_members_count(self, obj):
        return obj.members.count()


class LoyaltyMemberSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    email = serializers.SerializerMethodField()
    phone = serializers.SerializerMethodField()
    tier_name = serializers.CharField(source='tier.name', read_only=True, default='')
    tier_level = serializers.CharField(source='tier.level', read_only=True, default='bronze')
    customer_id = serializers.SerializerMethodField()
    customer_code = serializers.SerializerMethodField()
    customer_status = serializers.SerializerMethodField()

    class Meta:
        model = LoyaltyMember
        fields = [
            'id', 'customer_id', 'customer_code', 'customer_status',
            'name', 'email', 'phone',
            'tier', 'tier_name', 'tier_level',
            'current_points', 'lifetime_points',
            'total_spent', 'total_payments', 'redemptions_count',
            'joined_date', 'last_activity',
        ]

    def get_name(self, obj):
        if obj.customer:
            return obj.customer.full_name
        if obj.hotspot_client:
            return obj.hotspot_client.canonical_username or 'Hotspot User'
        return 'Unknown'

    def get_email(self, obj):
        if obj.customer:
            return obj.customer.email or ''
        return ''

    def get_phone(self, obj):
        if obj.customer and hasattr(obj.customer, 'user'):
            return getattr(obj.customer.user, 'phone_number', '') or ''
        if obj.hotspot_client:
            p = obj.hotspot_client.canonical_phone or ''
            # Don't return MAC addresses as phone numbers
            return p if not p.startswith('MAC-') else ''
        return ''

    def get_customer_id(self, obj):
        if obj.customer:
            return obj.customer.id
        return None

    def get_customer_code(self, obj):
        if obj.customer:
            return obj.customer.customer_code
        if obj.hotspot_client:
            return obj.hotspot_client.canonical_username or ''
        return ''

    def get_customer_status(self, obj):
        if obj.customer:
            return obj.customer.status
        return 'hotspot'


class PointsTransactionSerializer(serializers.ModelSerializer):
    member_name = serializers.SerializerMethodField()
    member_id = serializers.IntegerField(source='member.id', read_only=True)
    reward_name = serializers.CharField(source='reward.name', read_only=True, default=None)

    class Meta:
        model = PointsTransaction
        fields = [
            'id', 'member_id', 'member_name',
            'transaction_type', 'points', 'description',
            'reward', 'reward_name', 'expires_at',
            'created_at',
        ]

    def get_member_name(self, obj):
        if obj.member.customer:
            return obj.member.customer.full_name
        if obj.member.hotspot_client:
            return obj.member.hotspot_client.canonical_username or 'Hotspot User'
        return 'Unknown'


class LoyaltyRewardSerializer(serializers.ModelSerializer):
    is_available = serializers.BooleanField(read_only=True)

    class Meta:
        model = LoyaltyReward
        fields = [
            'id', 'name', 'description', 'points_cost', 'category',
            'status', 'stock_quantity', 'redemption_count', 'valid_until',
            'image', 'voucher_batch_id', 'credit_amount', 'is_available',
            'hotspot_reward_minutes', 'hotspot_reward_speed_mbps',
        ]


class LoyaltyRewardWriteSerializer(serializers.ModelSerializer):
    hotspot_reward_speed_mbps = serializers.CharField(
        required=False,
        allow_blank=True,
        default='5',
    )
    
    class Meta:
        model = LoyaltyReward
        fields = [
            'name', 'description', 'points_cost', 'category',
            'status', 'stock_quantity', 'valid_until', 'image',
            'voucher_batch_id', 'credit_amount',
            'hotspot_reward_minutes', 'hotspot_reward_speed_mbps',
        ]
    
    # Add this method to ensure empty strings are converted to the default '5'
    def validate_hotspot_reward_speed_mbps(self, value):
        return value if value else '5'


class PointsRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = PointsRule
        fields = [
            'id', 'name', 'trigger', 'points', 'description',
            'is_active', 'min_amount',
        ]


class AwardPointsSerializer(serializers.Serializer):
    member_id = serializers.IntegerField()
    points = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=200, required=False, default='Manual award')

    def validate_member_id(self, value):
        if not LoyaltyMember.objects.filter(id=value).exists():
            raise serializers.ValidationError('Member not found')
        return value


class BulkAwardPointsSerializer(serializers.Serializer):
    member_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)
    points = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=200, required=False, default='Bulk award')


class RedeemRewardSerializer(serializers.Serializer):
    member_id = serializers.IntegerField()
    reward_id = serializers.IntegerField()

    def validate(self, attrs):
        try:
            member = LoyaltyMember.objects.get(id=attrs['member_id'])
        except LoyaltyMember.DoesNotExist:
            raise serializers.ValidationError({'member_id': 'Member not found'})
        try:
            reward = LoyaltyReward.objects.get(id=attrs['reward_id'])
        except LoyaltyReward.DoesNotExist:
            raise serializers.ValidationError({'reward_id': 'Reward not found'})

        if not reward.is_available:
            raise serializers.ValidationError({'reward_id': 'Reward is not available'})
        if member.current_points < reward.points_cost:
            raise serializers.ValidationError(
                {'member_id': f'Insufficient points ({member.current_points} < {reward.points_cost})'}
            )
        attrs['member'] = member
        attrs['reward'] = reward
        return attrs


class AwardVoucherSerializer(serializers.Serializer):
    """Award a hotspot voucher to a loyalty member and send via SMS."""
    member_id = serializers.IntegerField()
    voucher_batch_id = serializers.IntegerField(required=False, help_text='Optional batch to pick voucher from')
    send_sms = serializers.BooleanField(default=True)