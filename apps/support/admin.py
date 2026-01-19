from django.contrib import admin
from django.utils.html import format_html
from .models import SupportTicket, SupportTicketMessage


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ['ticket_number', 'subject', 'customer', 'status_display', 
                   'priority_display', 'category', 'assigned_to', 'created_at', 'age_display']
    list_filter = ['status', 'priority', 'category', 'created_at']
    search_fields = ['ticket_number', 'subject', 'description', 'customer__user__email']
    readonly_fields = ['ticket_number', 'created_at', 'updated_at', 'first_response_at', 'resolved_at']
    list_per_page = 50
    
    fieldsets = (
        ('Ticket Information', {
            'fields': ('ticket_number', 'subject', 'description', 'status', 'priority', 'category')
        }),
        ('Assignment', {
            'fields': ('customer', 'assigned_to')
        }),
        ('SLA & Timestamps', {
            'fields': ('sla_breached', 'first_response_at', 'resolved_at', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def status_display(self, obj):
        colors = {
            'open': 'blue',
            'in_progress': 'orange',
            'pending': 'yellow',
            'resolved': 'green',
            'closed': 'gray'
        }
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            colors.get(obj.status, 'black'),
            obj.get_status_display()
        )
    status_display.short_description = 'Status'
    
    def priority_display(self, obj):
        colors = {
            'low': 'green',
            'medium': 'orange',
            'high': 'red',
            'urgent': 'darkred'
        }
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            colors.get(obj.priority, 'black'),
            obj.get_priority_display()
        )
    priority_display.short_description = 'Priority'
    
    def age_display(self, obj):
        age_hours = (timezone.now() - obj.created_at).total_seconds() / 3600
        if age_hours < 24:
            return f"{int(age_hours)}h"
        else:
            return f"{int(age_hours/24)}d"
    age_display.short_description = 'Age'


@admin.register(SupportTicketMessage)
class SupportTicketMessageAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'sender_type', 'sender', 'message_preview', 'created_at']
    list_filter = ['sender_type', 'is_internal', 'created_at']
    search_fields = ['message', 'ticket__ticket_number']
    readonly_fields = ['created_at']
    list_per_page = 50
    
    def message_preview(self, obj):
        if len(obj.message) > 50:
            return f"{obj.message[:50]}..."
        return obj.message
    message_preview.short_description = 'Message'
