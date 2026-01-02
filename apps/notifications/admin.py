from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from .models import (
    NotificationTemplate, 
    Notification, 
    AlertRule,
    NotificationPreference,
    BulkNotification,
    NotificationLog
)

@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'notification_type', 'trigger_event', 
        'is_active', 'priority', 'created_at'
    ]
    list_filter = ['notification_type', 'trigger_event', 'is_active']
    search_fields = ['name', 'subject', 'message_template']
    readonly_fields = ['created_at', 'updated_at']
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'notification_type', 'trigger_event', 'is_active')
        }),
        ('Content', {
            'fields': ('subject', 'message_template', 'available_variables')
        }),
        ('Settings', {
            'fields': ('priority',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    actions = ['activate_templates', 'deactivate_templates']
    
    def activate_templates(self, request, queryset):
        queryset.update(is_active=True)
        self.message_user(request, f"{queryset.count()} templates activated.")
    activate_templates.short_description = "Activate selected templates"
    
    def deactivate_templates(self, request, queryset):
        queryset.update(is_active=False)
        self.message_user(request, f"{queryset.count()} templates deactivated.")
    deactivate_templates.short_description = "Deactivate selected templates"

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'notification_type', 'recipient_info', 
        'subject_short', 'status_badge', 'priority', 
        'sent_at_short', 'created_at_short'
    ]
    list_filter = ['notification_type', 'status', 'priority', 'created_at']
    search_fields = [
        'subject', 'message', 'recipient_email', 
        'recipient_phone', 'user__email'
    ]
    readonly_fields = [
        'created_at', 'updated_at', 'sent_at', 
        'delivered_at', 'read_at', 'error_message',
        'retry_count', 'metadata_display'
    ]
    fieldsets = (
        ('Recipient', {
            'fields': ('user', 'recipient_email', 'recipient_phone', 'recipient_device_token')
        }),
        ('Content', {
            'fields': ('notification_type', 'template', 'subject', 'message')
        }),
        ('Status', {
            'fields': ('status', 'priority', 'error_message')
        }),
        ('Delivery Tracking', {
            'fields': ('sent_at', 'delivered_at', 'read_at', 'retry_count', 'max_retries')
        }),
        ('Metadata', {
            'fields': ('metadata_display',),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    actions = ['retry_failed', 'mark_as_read', 'mark_as_unread']
    
    def recipient_info(self, obj):
        if obj.user:
            url = reverse('admin:auth_user_change', args=[obj.user.id])
            return format_html(
                '<a href="{}">{}</a>',
                url,
                obj.user.email
            )
        elif obj.recipient_email:
            return obj.recipient_email
        elif obj.recipient_phone:
            return obj.recipient_phone
        return '-'
    recipient_info.short_description = 'Recipient'
    
    def subject_short(self, obj):
        if obj.subject:
            return obj.subject[:50] + ('...' if len(obj.subject) > 50 else '')
        return '-'
    subject_short.short_description = 'Subject'
    
    def status_badge(self, obj):
        colors = {
            'pending': 'gray',
            'sent': 'blue',
            'delivered': 'green',
            'failed': 'red',
            'read': 'purple'
        }
        color = colors.get(obj.status, 'gray')
        return format_html(
            '<span style="padding: 2px 8px; border-radius: 12px; '
            f'background-color: {color}; color: white;">{obj.get_status_display()}</span>'
        )
    status_badge.short_description = 'Status'
    
    def sent_at_short(self, obj):
        if obj.sent_at:
            return obj.sent_at.strftime('%Y-%m-%d %H:%M')
        return '-'
    sent_at_short.short_description = 'Sent At'
    
    def created_at_short(self, obj):
        return obj.created_at.strftime('%Y-%m-%d %H:%M')
    created_at_short.short_description = 'Created'
    
    def metadata_display(self, obj):
        import json
        if obj.metadata:
            return format_html('<pre>{}</pre>', json.dumps(obj.metadata, indent=2))
        return '-'
    metadata_display.short_description = 'Metadata'
    
    def retry_failed(self, request, queryset):
        from .services import NotificationManager
        manager = NotificationManager()
        count = 0
        for notification in queryset.filter(status='failed'):
            if notification.should_retry():
                success = manager.send_notification(notification)
                if success:
                    count += 1
        self.message_user(request, f"{count} notifications retried.")
    retry_failed.short_description = "Retry failed notifications"
    
    def mark_as_read(self, request, queryset):
        from django.utils import timezone
        queryset.update(status='read', read_at=timezone.now())
        self.message_user(request, f"{queryset.count()} notifications marked as read.")
    mark_as_read.short_description = "Mark as read"
    
    def mark_as_unread(self, request, queryset):
        queryset.update(status='sent', read_at=None)
        self.message_user(request, f"{queryset.count()} notifications marked as unread.")
    mark_as_unread.short_description = "Mark as unread"

@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'alert_type', 'is_active', 
        'condition_summary', 'last_triggered_short', 'created_at_short'
    ]
    list_filter = ['alert_type', 'is_active', 'created_at']
    search_fields = ['name', 'description', 'model_name', 'field_name']
    readonly_fields = ['created_at', 'updated_at', 'last_checked', 'last_triggered']
    filter_horizontal = ['notification_templates', 'specific_users']
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'description', 'alert_type', 'is_active')
        }),
        ('Condition', {
            'fields': ('model_name', 'field_name', 'condition_type', 'condition_value')
        }),
        ('Notification', {
            'fields': ('notification_templates', 'target_roles', 'specific_users')
        }),
        ('Schedule', {
            'fields': ('check_interval', 'enabled_days', 'enabled_hours', 'cooldown_minutes')
        }),
        ('Tracking', {
            'fields': ('last_checked', 'last_triggered'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    actions = ['activate_rules', 'deactivate_rules']
    
    def condition_summary(self, obj):
        return f"{obj.field_name} {obj.get_condition_type_display()} {obj.condition_value}"
    condition_summary.short_description = 'Condition'
    
    def last_triggered_short(self, obj):
        if obj.last_triggered:
            return obj.last_triggered.strftime('%Y-%m-%d %H:%M')
        return 'Never'
    last_triggered_short.short_description = 'Last Triggered'
    
    def created_at_short(self, obj):
        return obj.created_at.strftime('%Y-%m-%d %H:%M')
    created_at_short.short_description = 'Created'
    
    def activate_rules(self, request, queryset):
        queryset.update(is_active=True)
        self.message_user(request, f"{queryset.count()} alert rules activated.")
    activate_rules.short_description = "Activate selected rules"
    
    def deactivate_rules(self, request, queryset):
        queryset.update(is_active=False)
        self.message_user(request, f"{queryset.count()} alert rules deactivated.")
    deactivate_rules.short_description = "Deactivate selected rules"

@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = [
        'user_email', 'receive_email', 'receive_sms', 
        'receive_push', 'quiet_hours_enabled', 'updated_at_short'
    ]
    search_fields = ['user__email', 'user__first_name', 'user__last_name']
    readonly_fields = ['updated_at']
    list_filter = [
        'receive_email', 'receive_sms', 'receive_push', 
        'quiet_hours_enabled', 'updated_at'
    ]
    fieldsets = (
        ('User', {
            'fields': ('user',)
        }),
        ('Channel Preferences', {
            'fields': (
                'receive_email', 'receive_sms', 'receive_push', 
                'receive_in_app', 'preferred_language'
            )
        }),
        ('Category Preferences', {
            'fields': (
                'billing_notifications', 'service_notifications',
                'support_notifications', 'marketing_notifications',
                'system_notifications'
            )
        }),
        ('Quiet Hours', {
            'fields': ('quiet_hours_enabled', 'quiet_start_time', 'quiet_end_time')
        }),
        ('Limits', {
            'fields': ('daily_notification_limit',)
        }),
        ('Timestamp', {
            'fields': ('updated_at',),
            'classes': ('collapse',)
        })
    )
    
    def user_email(self, obj):
        url = reverse('admin:auth_user_change', args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.email)
    user_email.short_description = 'User'
    
    def updated_at_short(self, obj):
        return obj.updated_at.strftime('%Y-%m-%d %H:%M')
    updated_at_short.short_description = 'Updated'

@admin.register(BulkNotification)
class BulkNotificationAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'notification_type', 'target_segment', 
        'status_badge', 'total_recipients', 'scheduled_for_short',
        'created_by_email', 'created_at_short'
    ]
    list_filter = ['notification_type', 'target_segment', 'status', 'created_at']
    search_fields = ['name', 'subject', 'message', 'created_by__email']
    readonly_fields = [
        'started_at', 'completed_at', 'total_recipients',
        'sent_count', 'failed_count', 'created_at', 'updated_at'
    ]
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'notification_type', 'status')
        }),
        ('Content', {
            'fields': ('subject', 'message')
        }),
        ('Target Audience', {
            'fields': ('target_segment', 'custom_recipients')
        }),
        ('Schedule', {
            'fields': ('scheduled_for',)
        }),
        ('Statistics', {
            'fields': (
                'total_recipients', 'sent_count', 'failed_count',
                'started_at', 'completed_at'
            ),
            'classes': ('collapse',)
        }),
        ('Creator', {
            'fields': ('created_by',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    actions = ['cancel_scheduled', 'mark_as_completed']
    
    def status_badge(self, obj):
        colors = {
            'draft': 'gray',
            'scheduled': 'blue',
            'processing': 'yellow',
            'completed': 'green',
            'failed': 'red',
            'cancelled': 'orange'
        }
        color = colors.get(obj.status, 'gray')
        return format_html(
            '<span style="padding: 2px 8px; border-radius: 12px; '
            f'background-color: {color}; color: white;">{obj.get_status_display()}</span>'
        )
    status_badge.short_description = 'Status'
    
    def scheduled_for_short(self, obj):
        if obj.scheduled_for:
            return obj.scheduled_for.strftime('%Y-%m-%d %H:%M')
        return '-'
    scheduled_for_short.short_description = 'Scheduled For'
    
    def created_by_email(self, obj):
        if obj.created_by:
            url = reverse('admin:auth_user_change', args=[obj.created_by.id])
            return format_html('<a href="{}">{}</a>', url, obj.created_by.email)
        return '-'
    created_by_email.short_description = 'Created By'
    
    def created_at_short(self, obj):
        return obj.created_at.strftime('%Y-%m-%d %H:%M')
    created_at_short.short_description = 'Created'
    
    def cancel_scheduled(self, request, queryset):
        from django.utils import timezone
        count = 0
        for notification in queryset.filter(status__in=['scheduled', 'processing']):
            notification.status = 'cancelled'
            notification.save()
            count += 1
        self.message_user(request, f"{count} bulk notifications cancelled.")
    cancel_scheduled.short_description = "Cancel scheduled notifications"
    
    def mark_as_completed(self, request, queryset):
        from django.utils import timezone
        count = 0
        for notification in queryset.filter(status='processing'):
            notification.status = 'completed'
            notification.completed_at = timezone.now()
            notification.save()
            count += 1
        self.message_user(request, f"{count} bulk notifications marked as completed.")
    mark_as_completed.short_description = "Mark as completed"

@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'notification_link', 'action', 
        'user_email', 'timestamp_short', 'ip_address'
    ]
    list_filter = ['action', 'timestamp']
    search_fields = [
        'notification__subject', 'user__email', 
        'details', 'ip_address'
    ]
    readonly_fields = ['timestamp', 'ip_address', 'details_display']
    fieldsets = (
        ('Basic Information', {
            'fields': ('notification', 'user', 'action')
        }),
        ('Details', {
            'fields': ('details_display',)
        }),
        ('Metadata', {
            'fields': ('timestamp', 'ip_address'),
            'classes': ('collapse',)
        })
    )
    
    def notification_link(self, obj):
        if obj.notification:
            url = reverse('admin:notifications_notification_change', args=[obj.notification.id])
            return format_html('<a href="{}">Notification #{}</a>', url, obj.notification.id)
        return '-'
    notification_link.short_description = 'Notification'
    
    def user_email(self, obj):
        if obj.user:
            url = reverse('admin:auth_user_change', args=[obj.user.id])
            return format_html('<a href="{}">{}</a>', url, obj.user.email)
        return 'System'
    user_email.short_description = 'User'
    
    def timestamp_short(self, obj):
        return obj.timestamp.strftime('%Y-%m-%d %H:%M:%S')
    timestamp_short.short_description = 'Timestamp'
    
    def details_display(self, obj):
        import json
        if obj.details:
            return format_html('<pre>{}</pre>', json.dumps(obj.details, indent=2))
        return '-'
    details_display.short_description = 'Details'