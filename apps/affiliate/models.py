import secrets

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


def generate_referral_code():
    return secrets.token_hex(6).upper()


class AffiliateAccount(models.Model):
    STATUS_CHOICES = (
        ("active", "Active"),
        ("inactive", "Inactive"),
        ("suspended", "Suspended"),
    )
    TIER_CHOICES = (
        ("bronze", "Bronze"),
        ("silver", "Silver"),
        ("gold", "Gold"),
    )
    PAYMENT_CHOICES = (("mpesa", "M-Pesa"), ("bank", "Bank transfer"))

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="affiliate_account",
    )
    referral_code = models.CharField(max_length=16, unique=True, db_index=True, default=generate_referral_code)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="active", db_index=True)
    country = models.CharField(max_length=80)
    currency = models.CharField(max_length=3, default="KES")
    tier = models.CharField(max_length=12, choices=TIER_CHOICES, default="bronze")
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    payment_method = models.CharField(max_length=12, choices=PAYMENT_CHOICES, default="mpesa")
    mpesa_phone = models.CharField(max_length=20, blank=True)
    mpesa_name = models.CharField(max_length=120, blank=True)
    bank_name = models.CharField(max_length=120, blank=True)
    bank_account = models.CharField(max_length=120, blank=True)
    bank_branch = models.CharField(max_length=120, blank=True)
    payment_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.user.email} ({self.referral_code})"


class AffiliateClick(models.Model):
    affiliate = models.ForeignKey(AffiliateAccount, on_delete=models.CASCADE, related_name="clicks")
    attribution_token = models.UUIDField(unique=True, db_index=True)
    source = models.CharField(max_length=100, default="Direct")
    landing_url = models.URLField(max_length=1000, blank=True)
    referrer = models.URLField(max_length=1000, blank=True)
    ip_hash = models.CharField(max_length=64, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=("affiliate", "created_at"),
                name="affiliate_a_affilia_58b8bf_idx",
            )
        ]


class AffiliateReferral(models.Model):
    STATUS_CHOICES = (
        ("pending", "Pending review"),
        ("paid", "Commission paid"),
        ("churned", "Rejected or churned"),
    )

    affiliate = models.ForeignKey(AffiliateAccount, on_delete=models.PROTECT, related_name="referrals")
    click = models.ForeignKey(AffiliateClick, on_delete=models.SET_NULL, null=True, blank=True, related_name="signups")
    company = models.ForeignKey("core.Company", on_delete=models.SET_NULL, null=True, blank=True)
    lead = models.ForeignKey("core.Lead", on_delete=models.SET_NULL, null=True, blank=True)
    signup_email = models.EmailField()
    company_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending", db_index=True)
    reward_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Set manually by a superadmin.",
    )
    currency = models.CharField(max_length=3, default="KES")
    admin_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("signup_email",),
                name="unique_affiliate_signup_email",
            )
        ]


class AffiliatePayout(models.Model):
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    )

    affiliate = models.ForeignKey(AffiliateAccount, on_delete=models.PROTECT, related_name="payouts")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0.01)])
    currency = models.CharField(max_length=3, default="KES")
    method = models.CharField(max_length=12, choices=AffiliateAccount.PAYMENT_CHOICES)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="pending", db_index=True)
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="affiliate_payouts_created",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
