from django.contrib import admin
from .models import (
    LoyaltySettings,
    LoyaltyTier,
    LoyaltyMember,
    LoyaltyReward,
    PointsTransaction,
    PointsRule,
)


@admin.register(LoyaltySettings)
class LoyaltySettingsAdmin(admin.ModelAdmin):
    list_display = ('points_per_currency', 'signup_bonus', 'referral_bonus', 'points_expiry_enabled')


@admin.register(LoyaltyTier)
class LoyaltyTierAdmin(admin.ModelAdmin):
    list_display = ('name', 'level', 'min_points', 'max_points', 'points_multiplier', 'members_count')
    ordering = ('min_points',)

    def members_count(self, obj):
        return obj.members.count()


@admin.register(LoyaltyMember)
class LoyaltyMemberAdmin(admin.ModelAdmin):
    list_display = ('customer', 'tier', 'current_points', 'lifetime_points', 'total_spent', 'total_payments')
    list_filter = ('tier',)
    search_fields = ('customer__user__first_name', 'customer__user__last_name', 'customer__customer_code')
    raw_id_fields = ('customer',)


@admin.register(LoyaltyReward)
class LoyaltyRewardAdmin(admin.ModelAdmin):
    list_display = ('name', 'points_cost', 'category', 'status', 'stock_quantity', 'redemption_count')
    list_filter = ('status', 'category')


@admin.register(PointsTransaction)
class PointsTransactionAdmin(admin.ModelAdmin):
    list_display = ('member', 'transaction_type', 'points', 'description', 'created_at')
    list_filter = ('transaction_type',)
    raw_id_fields = ('member',)


@admin.register(PointsRule)
class PointsRuleAdmin(admin.ModelAdmin):
    list_display = ('name', 'trigger', 'points', 'is_active')
    list_filter = ('trigger', 'is_active')
