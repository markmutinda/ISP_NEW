from django.contrib import admin
from .models import SMSMessage, SMSTemplate, SMSCampaign


@admin.register(SMSTemplate)
class SMSTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'usage_count', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'content')
    readonly_fields = ('usage_count', 'created_at', 'updated_at')


@admin.register(SMSCampaign)
class SMSCampaignAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'recipient_count', 'delivered_count', 'failed_count', 'created_at')
    list_filter = ('status',)
    search_fields = ('name',)
    readonly_fields = ('recipient_count', 'delivered_count', 'failed_count', 'started_at', 'completed_at')


@admin.register(SMSMessage)
class SMSMessageAdmin(admin.ModelAdmin):
    list_display = ('recipient', 'status', 'type', 'cost', 'sent_at', 'created_at')
    list_filter = ('status', 'type', 'provider')
    search_fields = ('recipient', 'message', 'error_message')
    readonly_fields = ('provider_message_id', 'cost', 'sent_at', 'delivered_at', 'error_message', 'created_at')
    date_hierarchy = 'created_at'
