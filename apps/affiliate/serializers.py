from decimal import Decimal

from django.contrib.auth.password_validation import validate_password
from django.conf import settings
from django.db import transaction
from rest_framework import serializers

from apps.core.models import User

from .models import AffiliateAccount, AffiliatePayout, AffiliateReferral


COUNTRY_CURRENCIES = {
    "Kenya": "KES",
    "Uganda": "UGX",
    "Tanzania": "TZS",
    "Rwanda": "RWF",
    "Ethiopia": "ETB",
    "Burundi": "BIF",
    "South Sudan": "SSP",
    "Nigeria": "NGN",
    "Ghana": "GHS",
    "South Africa": "ZAR",
}


class AffiliateRegisterSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=180)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=17)
    country = serializers.CharField(max_length=80)
    password = serializers.CharField(write_only=True, validators=[validate_password])

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_phone(self, value):
        value = value.replace(" ", "")
        if User.objects.filter(phone_number=value).exists():
            raise serializers.ValidationError("An account with this phone number already exists.")
        return value

    def create(self, validated_data):
        names = validated_data["full_name"].strip().split(maxsplit=1)
        with transaction.atomic():
            user = User.objects.create_user(
                email=validated_data["email"],
                password=validated_data["password"],
                phone_number=validated_data["phone"],
                first_name=names[0],
                last_name=names[1] if len(names) > 1 else "",
                role="customer",
                is_active=True,
                is_verified=False,
            )
            return AffiliateAccount.objects.create(
                user=user,
                country=validated_data["country"],
                currency=COUNTRY_CURRENCIES.get(validated_data["country"], "USD"),
            )


def affiliate_user_data(account):
    user = account.user
    full_name = user.get_full_name().strip() or user.email
    return {
        "id": account.id,
        "email": user.email,
        "full_name": full_name,
        "phone": user.phone_number,
        "country": account.country,
        "currency": account.currency,
        "referral_code": account.referral_code,
        "referral_link": f"{getattr(settings, 'FRONTEND_URL', 'https://netily.co.ke').rstrip('/')}/r/{account.referral_code}",
        "is_verified": account.is_verified,
        "tier": account.tier,
        "status": account.status,
        "created_at": account.created_at.isoformat(),
    }


def referral_data(referral):
    return {
        "id": referral.id,
        "isp_name": referral.company_name or referral.signup_email,
        "company": referral.company_name,
        "signup_date": referral.created_at.isoformat(),
        "status": referral.status,
        "reward_amount": float(referral.reward_amount),
        "currency": referral.currency,
        "admin_notes": referral.admin_notes,
    }


def payout_data(payout):
    return {
        "id": payout.id,
        "date": payout.created_at.isoformat(),
        "amount": float(payout.amount),
        "currency": payout.currency,
        "method": payout.method,
        "status": payout.status,
        "reference": payout.reference,
        "notes": payout.notes,
    }


class AffiliateAccountAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = AffiliateAccount
        fields = ("status", "tier", "payment_verified")


class AffiliateReferralAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = AffiliateReferral
        fields = ("status", "reward_amount", "currency", "admin_notes")

    def validate_reward_amount(self, value):
        if value < Decimal("0"):
            raise serializers.ValidationError("Reward amount cannot be negative.")
        return value


class AffiliatePayoutAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = AffiliatePayout
        fields = ("amount", "currency", "method", "status", "reference", "notes")
