from django.contrib import admin
# from .models import BandwidthProfile, TrafficRule, DataUsage, BandwidthAlert, SpeedTestResult


# @admin.register(BandwidthProfile)
# class BandwidthProfileAdmin(admin.ModelAdmin):
#     list_display = ['name', 'profile_type', 'download_speed', 'upload_speed', 'data_cap',
#                     'monthly_price', 'is_active']
#     list_filter = ['profile_type', 'is_active', 'company']
#     search_fields = ['name', 'description']
#     list_editable = ['is_active', 'monthly_price']


# @admin.register(TrafficRule)
# class TrafficRuleAdmin(admin.ModelAdmin):
#     list_display = ['name', 'rule_type', 'priority_level', 'is_active',
#                     'is_applied', 'applied_at']
#     list_filter = ['rule_type', 'priority_level', 'is_active',
#                    'is_applied', 'company']
#     search_fields = ['name', 'application_protocol']
#     list_editable = ['is_active']


# @admin.register(DataUsage)
# class DataUsageAdmin(admin.ModelAdmin):
#     list_display = ['customer', 'period_start', 'period_end',
#                     'download_gb', 'upload_gb', 'total_gb', 'usage_percentage']
#     list_filter = ['period_start', 'period_end', 'is_over_limit']
#     search_fields = ['customer__customer_code', 'customer__user__email']
#     readonly_fields = ['download_gb', 'upload_gb',
#                        'total_gb', 'usage_percentage']


# @admin.register(BandwidthAlert)
# class BandwidthAlertAdmin(admin.ModelAdmin):
#     list_display = ['alert_type', 'alert_level', 'customer',
#                     'is_triggered', 'triggered_at',
#                     'acknowledged', 'resolved']
#     list_filter = ['alert_type', 'alert_level', 'is_triggered',
#                    'acknowledged', 'resolved', 'company']
#     search_fields = ['customer__customer_code', 'message']
#     readonly_fields = ['triggered_at',
#                        'acknowledged_at', 'resolved_at']


# @admin.register(SpeedTestResult)
# class SpeedTestResultAdmin(admin.ModelAdmin):
#     list_display = ['customer', 'download_speed', 'upload_speed',
#                     'latency', 'test_time', 'is_valid']
#     list_filter = ['test_time', 'is_valid', 'company']
#     search_fields = ['customer__customer_code', 'server_name']

