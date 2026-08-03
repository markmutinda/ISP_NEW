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
        "referral_link": f"{getattr(settings, 'FRONTEND_URL', 'https://netily.co.ke').rstrip('/')}/affiliate/{account.referral_code}",
        "is_verified": account.is_verified,
        "tier": account.tier,
        "status": account.status,
        "created_at": account.created_at.isoformat(),
    }


def referral_data(referral):
    click = referral.click
    lead = getattr(referral, "lead", None)
    attribution_type = "tracked_click" if click else "lead_form" if lead else "manual"
    return {
        "id": referral.id,
        "isp_name": referral.company_name or referral.signup_email,
        "company": referral.company_name,
        "signup_email": referral.signup_email,
        "signup_date": referral.created_at.isoformat(),
        "status": referral.status,
        "reward_amount": float(referral.reward_amount),
        "currency": referral.currency,
        "admin_notes": referral.admin_notes,
        "attribution_type": attribution_type,
        "click_id": click.id if click else None,
        "clicked_at": click.created_at.isoformat() if click else None,
        "lead_id": lead.id if lead else None,
        "source": click.source if click else "Referral form" if lead else "Manual",
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
    full_name = serializers.CharField(max_length=180, required=False)
    email = serializers.EmailField(required=False)
    phone = serializers.CharField(max_length=17, required=False)

    class Meta:
        model = AffiliateAccount
        fields = (
            "full_name",
            "email",
            "phone",
            "status",
            "tier",
            "country",
            "currency",
            "referral_code",
            "is_verified",
            "payment_verified",
        )

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exclude(pk=self.instance.user_id).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_phone(self, value):
        value = value.replace(" ", "")
        if User.objects.filter(phone_number=value).exclude(pk=self.instance.user_id).exists():
            raise serializers.ValidationError("An account with this phone number already exists.")
        return value

    def validate_referral_code(self, value):
        value = value.strip().upper()
        if AffiliateAccount.objects.filter(referral_code=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError("This referral code is already in use.")
        return value

    def update(self, instance, validated_data):
        from django.utils import timezone

        user = instance.user
        full_name = validated_data.pop("full_name", None)
        email = validated_data.pop("email", None)
        phone = validated_data.pop("phone", None)
        if full_name is not None:
            names = full_name.strip().split(maxsplit=1)
            user.first_name = names[0] if names else ""
            user.last_name = names[1] if len(names) > 1 else ""
        if email is not None:
            user.email = email
        if phone is not None:
            user.phone_number = phone
        if validated_data.get("status") == "active":
            user.is_active = True
        if "is_verified" in validated_data:
            user.is_verified = bool(validated_data["is_verified"])
            instance.verified_at = timezone.now() if validated_data["is_verified"] else None
        user.save()
        return super().update(instance, validated_data)


class AffiliateAccountAdminCreateSerializer(AffiliateRegisterSerializer):
    status = serializers.ChoiceField(choices=AffiliateAccount.STATUS_CHOICES, default="active")
    tier = serializers.ChoiceField(choices=AffiliateAccount.TIER_CHOICES, default="bronze")
    is_verified = serializers.BooleanField(default=False)

    def create(self, validated_data):
        status_value = validated_data.pop("status")
        tier = validated_data.pop("tier")
        is_verified = validated_data.pop("is_verified")
        account = super().create(validated_data)
        account.status = status_value
        account.tier = tier
        account.is_verified = is_verified
        if is_verified:
            from django.utils import timezone

            account.verified_at = timezone.now()
            account.user.is_verified = True
            account.user.save(update_fields=["is_verified"])
        account.save(update_fields=["status", "tier", "is_verified", "verified_at", "updated_at"])
        return account


class AffiliateReferralAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = AffiliateReferral
        fields = ("status", "reward_amount", "currency", "admin_notes")

    def validate_reward_amount(self, value):
        if value < Decimal("0"):
            raise serializers.ValidationError("Reward amount cannot be negative.")
        return value


class AffiliateReferralAdminCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = AffiliateReferral
        fields = ("signup_email", "company_name", "status", "reward_amount", "currency", "admin_notes")

    def validate_signup_email(self, value):
        value = value.strip().lower()
        if AffiliateReferral.objects.filter(signup_email__iexact=value).exists():
            raise serializers.ValidationError("This signup is already assigned to an affiliate.")
        return value


class AffiliatePayoutAdminSerializer(serializers.ModelSerializer):
    class Meta:
        model = AffiliatePayout
        fields = ("amount", "currency", "method", "status", "reference", "notes")
