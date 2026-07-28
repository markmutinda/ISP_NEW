from django.contrib import admin

from .models import AffiliateAccount, AffiliateClick, AffiliatePayout, AffiliateReferral


@admin.register(AffiliateAccount)
class AffiliateAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "referral_code", "status", "tier", "currency", "is_verified")
    list_filter = ("status", "tier", "is_verified", "payment_method")
    search_fields = ("user__email", "user__first_name", "user__last_name", "referral_code")


@admin.register(AffiliateClick)
class AffiliateClickAdmin(admin.ModelAdmin):
    list_display = ("affiliate", "source", "created_at")
    list_filter = ("source",)
    readonly_fields = ("attribution_token", "ip_hash", "created_at")


@admin.register(AffiliateReferral)
class AffiliateReferralAdmin(admin.ModelAdmin):
    list_display = ("affiliate", "company_name", "signup_email", "status", "reward_amount", "currency")
    list_filter = ("status", "currency")
    search_fields = ("signup_email", "company_name", "affiliate__referral_code")


@admin.register(AffiliatePayout)
class AffiliatePayoutAdmin(admin.ModelAdmin):
    list_display = ("affiliate", "amount", "currency", "method", "status", "reference", "created_at")
    list_filter = ("status", "method", "currency")
