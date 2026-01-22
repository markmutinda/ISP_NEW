"""
Serializers for Netily Platform Subscriptions
"""

from decimal import Decimal
from rest_framework import serializers
from django.utils import timezone

from .models import (
    NetilyPlan,
    CompanySubscription,
    SubscriptionPayment,
    ISPPayoutConfig,
    ISPSettlement,
    CommissionLedger,
)


class NetilyPlanSerializer(serializers.ModelSerializer):
    """Serializer for Netily subscription plans"""
    
    yearly_savings = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    yearly_discount_percent = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = NetilyPlan
        fields = [
            'id',
            'name',
            'code',
            'description',
            'tagline',
            'price_monthly',
            'price_yearly',
            'currency',
            'max_subscribers',
            'max_routers',
            'max_staff',
            'features',
            'is_active',
            'is_popular',
            'yearly_savings',
            'yearly_discount_percent',
        ]


class CompanySubscriptionSerializer(serializers.ModelSerializer):
    """Serializer for company subscription details"""
    
    plan = NetilyPlanSerializer(read_only=True)
    plan_id = serializers.PrimaryKeyRelatedField(
        queryset=NetilyPlan.objects.filter(is_active=True),
        source='plan',
        write_only=True
    )
    company_name = serializers.CharField(source='company.name', read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    days_remaining = serializers.IntegerField(read_only=True)
    current_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    
    # Trial fields
    is_on_trial = serializers.BooleanField(read_only=True)
    trial_days_remaining = serializers.IntegerField(read_only=True)
    trial_expired = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = CompanySubscription
        fields = [
            'id',
            'company_name',
            'plan',
            'plan_id',
            'billing_period',
            'current_period_start',
            'current_period_end',
            'status',
            'is_active',
            'days_remaining',
            'current_price',
            'cancel_at_period_end',
            # Trial fields
            'is_trial',
            'is_on_trial',
            'trial_started_at',
            'trial_ends_at',
            'trial_days_remaining',
            'trial_expired',
            'converted_from_trial_at',
            'created_at',
        ]
        read_only_fields = [
            'id',
            'current_period_start',
            'current_period_end',
            'status',
            'is_trial',
            'trial_started_at',
            'trial_ends_at',
            'converted_from_trial_at',
            'created_at',
        ]


class SubscriptionUsageSerializer(serializers.Serializer):
    """Serializer for subscription usage statistics"""
    
    plan_name = serializers.CharField()
    plan_code = serializers.CharField()
    
    # Current usage
    current_subscribers = serializers.IntegerField()
    current_routers = serializers.IntegerField()
    current_staff = serializers.IntegerField()
    
    # Limits from plan
    max_subscribers = serializers.IntegerField()
    max_routers = serializers.IntegerField()
    max_staff = serializers.IntegerField()
    
    # Percentages
    subscribers_usage_percent = serializers.IntegerField()
    routers_usage_percent = serializers.IntegerField()
    staff_usage_percent = serializers.IntegerField()
    
    # Warnings
    is_near_limit = serializers.BooleanField()
    warnings = serializers.ListField(child=serializers.CharField())
    
    # Trial status
    is_on_trial = serializers.BooleanField()
    trial_days_remaining = serializers.IntegerField()
    trial_expired = serializers.BooleanField()
    subscription_status = serializers.CharField()


class InitiateSubscriptionPaymentSerializer(serializers.Serializer):
    """Serializer for initiating subscription payment"""
    
    plan_id = serializers.CharField(required=True)
    payment_method = serializers.ChoiceField(
        choices=['mpesa_stk', 'mpesa_paybill', 'bank_transfer'],
        default='mpesa_stk'
    )
    phone_number = serializers.CharField(
        required=False,
        help_text="Required for M-Pesa STK push"
    )
    billing_period = serializers.ChoiceField(
        choices=['monthly', 'yearly'],
        default='monthly'
    )
    
    def validate(self, data):
        """Validate payment initiation data"""
        if data['payment_method'] == 'mpesa_stk' and not data.get('phone_number'):
            raise serializers.ValidationError({
                'phone_number': 'Phone number is required for M-Pesa STK push'
            })
        
        # Validate plan exists
        try:
            plan = NetilyPlan.objects.get(code=data['plan_id'], is_active=True)
            data['plan'] = plan
        except NetilyPlan.DoesNotExist:
            raise serializers.ValidationError({
                'plan_id': f"Plan '{data['plan_id']}' not found"
            })
        
        return data


class SubscriptionPaymentSerializer(serializers.ModelSerializer):
    """Serializer for subscription payment records"""
    
    company_name = serializers.CharField(source='subscription.company.name', read_only=True)
    plan_name = serializers.CharField(source='subscription.plan.name', read_only=True)
    
    class Meta:
        model = SubscriptionPayment
        fields = [
            'id',
            'company_name',
            'plan_name',
            'amount',
            'currency',
            'payment_method',
            'phone_number',
            'mpesa_receipt',
            'status',
            'failure_reason',
            'period_start',
            'period_end',
            'created_at',
            'completed_at',
        ]


class SubscriptionPaymentStatusSerializer(serializers.Serializer):
    """Serializer for payment status polling response"""
    
    payment_id = serializers.UUIDField()
    status = serializers.CharField()
    message = serializers.CharField()
    mpesa_receipt = serializers.CharField(allow_null=True)
    completed_at = serializers.DateTimeField(allow_null=True)


# ─────────────────────────────────────────────────────────────
#  ISP PAYOUT CONFIG SERIALIZERS
# ─────────────────────────────────────────────────────────────

class ISPPayoutConfigSerializer(serializers.ModelSerializer):
    """Serializer for ISP payout configuration"""
    
    company_name = serializers.CharField(source='company.name', read_only=True)
    payout_destination = serializers.CharField(read_only=True)
    
    # Don't expose full account numbers
    bank_account_number_masked = serializers.SerializerMethodField()
    mpesa_phone_masked = serializers.SerializerMethodField()
    
    class Meta:
        model = ISPPayoutConfig
        fields = [
            'id',
            'company_name',
            'payout_method',
            'mpesa_phone',
            'mpesa_phone_masked',
            'mpesa_name',
            'bank_code',
            'bank_name',
            'bank_account_number',
            'bank_account_number_masked',
            'bank_account_name',
            'bank_branch',
            'is_verified',
            'verified_at',
            'settlement_frequency',
            'minimum_payout',
            'pending_balance',
            'payout_destination',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'company_name',
            'is_verified',
            'verified_at',
            'pending_balance',
            'updated_at',
        ]
        extra_kwargs = {
            'mpesa_phone': {'write_only': True},
            'bank_account_number': {'write_only': True},
        }
    
    def get_bank_account_number_masked(self, obj):
        """Mask bank account number for display"""
        if obj.bank_account_number:
            return '*' * (len(obj.bank_account_number) - 4) + obj.bank_account_number[-4:]
        return None
    
    def get_mpesa_phone_masked(self, obj):
        """Mask phone number for display"""
        if obj.mpesa_phone:
            return obj.mpesa_phone[:4] + '***' + obj.mpesa_phone[-3:]
        return None
    
    def validate(self, data):
        """Validate payout configuration"""
        payout_method = data.get('payout_method', self.instance.payout_method if self.instance else 'mpesa_b2c')
        
        if payout_method == 'mpesa_b2c':
            if not data.get('mpesa_phone') and not (self.instance and self.instance.mpesa_phone):
                raise serializers.ValidationError({
                    'mpesa_phone': 'M-Pesa phone number is required'
                })
        
        elif payout_method == 'bank_transfer':
            required_fields = ['bank_code', 'bank_account_number', 'bank_account_name']
            for field in required_fields:
                if not data.get(field) and not (self.instance and getattr(self.instance, field)):
                    raise serializers.ValidationError({
                        field: f'{field.replace("_", " ").title()} is required for bank transfer'
                    })
        
        return data


class ISPPayoutConfigUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating ISP payout configuration"""
    
    class Meta:
        model = ISPPayoutConfig
        fields = [
            'payout_method',
            'mpesa_phone',
            'bank_code',
            'bank_name',
            'bank_account_number',
            'bank_account_name',
            'bank_branch',
            'settlement_frequency',
            'minimum_payout',
        ]
    
    def update(self, instance, validated_data):
        """Reset verification if critical fields change"""
        critical_fields = ['mpesa_phone', 'bank_account_number', 'payout_method']
        
        should_reset_verification = False
        for field in critical_fields:
            if field in validated_data and validated_data[field] != getattr(instance, field):
                should_reset_verification = True
                break
        
        if should_reset_verification:
            instance.is_verified = False
            instance.verified_at = None
        
        return super().update(instance, validated_data)


class VerifyPayoutSerializer(serializers.Serializer):
    """Serializer for payout verification request"""
    
    verification_code = serializers.CharField(
        required=False,
        help_text="Verification code from test payout (for confirming)"
    )


# ─────────────────────────────────────────────────────────────
#  SETTLEMENT SERIALIZERS
# ─────────────────────────────────────────────────────────────

class ISPSettlementSerializer(serializers.ModelSerializer):
    """Serializer for ISP settlement records"""
    
    company_name = serializers.CharField(source='company.name', read_only=True)
    
    class Meta:
        model = ISPSettlement
        fields = [
            'id',
            'company_name',
            'period_start',
            'period_end',
            'gross_amount',
            'commission_rate',
            'commission_amount',
            'net_amount',
            'payout_method',
            'payout_destination',
            'payout_reference',
            'status',
            'transaction_count',
            'created_at',
            'processed_at',
        ]


class SettlementSummarySerializer(serializers.Serializer):
    """Serializer for settlement summary on dashboard"""
    
    pending_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_collected_this_month = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_commission_this_month = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_earnings_this_month = serializers.DecimalField(max_digits=12, decimal_places=2)
    next_settlement_date = serializers.DateField(allow_null=True)
    settlement_frequency = serializers.CharField()
    payout_method = serializers.CharField()
    is_payout_configured = serializers.BooleanField()


class CommissionLedgerSerializer(serializers.ModelSerializer):
    """Serializer for commission ledger entries"""
    
    company_name = serializers.CharField(source='company.name', read_only=True)
    
    class Meta:
        model = CommissionLedger
        fields = [
            'id',
            'company_name',
            'payment_type',
            'payment_reference',
            'gross_amount',
            'commission_rate',
            'commission_amount',
            'isp_amount',
            'is_settled',
            'created_at',
        ]
