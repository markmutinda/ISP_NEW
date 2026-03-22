from django.contrib import admin
from .models import FUPPolicy, FUPPolicyPlan, FUPUsageWindow, FUPViolation, FUPThrottleState

@admin.register(FUPPolicy)
class FUPPolicyAdmin(admin.ModelAdmin):
    list_display = ('name', 'data_limit_gb', 'reset_period', 'status', 'is_active')
    list_filter = ('status', 'reset_period', 'is_active')

@admin.register(FUPUsageWindow)
class FUPUsageWindowAdmin(admin.ModelAdmin):
    list_display = ('customer', 'policy', 'status', 'usage_percent', 'is_throttled', 'period_end')
    list_filter = ('status', 'is_throttled')
    search_fields = ('customer__customer_code',)

@admin.register(FUPViolation)
class FUPViolationAdmin(admin.ModelAdmin):
    list_display = ('customer', 'policy', 'action_taken', 'occurred_at')
    search_fields = ('customer__customer_code',)

admin.site.register(FUPPolicyPlan)
admin.site.register(FUPThrottleState)