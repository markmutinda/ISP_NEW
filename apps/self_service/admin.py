from django.contrib import admin
from .models import CustomerSession, ServiceRequest, UsageAlert


@admin.register(CustomerSession)
class CustomerSessionAdmin(admin.ModelAdmin):
    list_display = ('customer', 'ip_address', 'login_time', 'logout_time', 'is_active')
    list_filter = ('is_active', 'login_time')
    search_fields = ('customer__name', 'ip_address', 'session_key')
    readonly_fields = ('login_time', 'logout_time', 'last_activity')
    fieldsets = (
        ('Session Information', {
            'fields': ('customer', 'session_key', 'ip_address', 'user_agent')
        }),
        ('Timestamps', {
            'fields': ('login_time', 'logout_time', 'last_activity')
        }),
        ('Status', {
            'fields': ('is_active',)
        }),
    )


@admin.register(ServiceRequest)
class ServiceRequestAdmin(admin.ModelAdmin):
    list_display = ('customer', 'request_type', 'subject', 'status', 'priority', 'created_at')
    list_filter = ('request_type', 'status', 'priority', 'created_at')
    search_fields = ('customer__name', 'subject', 'description')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        ('Request Information', {
            'fields': ('customer', 'request_type', 'subject', 'description', 'priority')
        }),
        ('Request Details', {
            'fields': ('current_plan', 'requested_plan', 'current_location', 'requested_location')
        }),
        ('Processing', {
            'fields': ('status', 'assigned_to', 'estimated_completion', 'actual_completion')
        }),
        ('Notes', {
            'fields': ('customer_notes', 'staff_notes')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at')
        }),
    )


@admin.register(UsageAlert)
class UsageAlertAdmin(admin.ModelAdmin):
    list_display = ('customer', 'alert_type', 'trigger_type', 'is_read', 'is_resolved', 'triggered_at')
    list_filter = ('alert_type', 'trigger_type', 'is_read', 'is_resolved', 'triggered_at')
    search_fields = ('customer__name', 'message')
    readonly_fields = ('triggered_at', 'resolved_at')
    fieldsets = (
        ('Alert Information', {
            'fields': ('customer', 'alert_type', 'trigger_type')
        }),
        ('Values', {
            'fields': ('threshold_value', 'current_value')
        }),
        ('Message', {
            'fields': ('message',)
        }),
        ('Status', {
            'fields': ('is_read', 'is_resolved')
        }),
        ('Timestamps', {
            'fields': ('triggered_at', 'resolved_at')
        }),
    )