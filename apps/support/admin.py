from django.contrib import admin
from django.utils.html import format_html

from .models import (
    TicketCategory, TicketStatus, Technician, Ticket,
    TicketMessage, TicketActivity, KnowledgeBaseArticle, FAQ,
    ServiceOutage
)


@admin.register(TicketCategory)
class TicketCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'priority', 'sla_hours', 'is_active', 'company']
    list_filter = ['is_active', 'company']
    search_fields = ['name', 'description']
    ordering = ['priority', 'name']


@admin.register(TicketStatus)
class TicketStatusAdmin(admin.ModelAdmin):
    list_display = ['name', 'is_open', 'is_closed', 'order', 'color_display', 'company']
    list_editable = ['order', 'is_open', 'is_closed']
    ordering = ['order']
    
    def color_display(self, obj):
        return format_html(
            '<span style="color: {};">{}</span>',
            obj.color,
            obj.color
        )
    color_display.short_description = 'Color'


@admin.register(Technician)
class TechnicianAdmin(admin.ModelAdmin):
    list_display = ['user', 'employee_id', 'department', 'is_available', 
                   'current_active_tickets', 'efficiency_score', 'company']
    list_filter = ['department', 'is_available', 'company']
    search_fields = ['user__first_name', 'user__last_name', 'user__email', 'employee_id']
    readonly_fields = ['current_active_tickets', 'average_rating', 'total_tickets_resolved',
                      'total_resolution_time', 'efficiency_score']


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ['ticket_number', 'subject', 'customer', 'priority_display',
                   'status', 'assigned_to', 'created_at', 'age_display', 'is_overdue_display', 'company']
    list_filter = ['priority', 'status', 'category', 'source_channel', 'created_at', 'company']
    search_fields = ['ticket_number', 'subject', 'description', 'customer__customer_code']
    readonly_fields = ['ticket_number', 'created_at', 'updated_at', 'first_response_at',
                      'resolved_at', 'closed_at', 'sla_due_at']
    
    def priority_display(self, obj):
        colors = {
            'low': 'green',
            'medium': 'orange',
            'high': 'red',
            'urgent': 'darkred',
            'critical': 'purple'
        }
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            colors.get(obj.priority, 'black'),
            obj.get_priority_display()
        )
    priority_display.short_description = 'Priority'
    
    def age_display(self, obj):
        age_hours = obj.age
        if age_hours < 24:
            return f"{int(age_hours)}h"
        else:
            return f"{int(age_hours/24)}d"
    age_display.short_description = 'Age'
    
    def is_overdue_display(self, obj):
        if obj.is_overdue:
            return format_html(
                '<span style="color: red; font-weight: bold;">OVERDUE</span>'
            )
        return ''
    is_overdue_display.short_description = 'Overdue'


@admin.register(TicketMessage)
class TicketMessageAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'sender', 'sender_type', 'is_internal', 'created_at', 'company']
    list_filter = ['sender_type', 'is_internal', 'created_at', 'company']
    search_fields = ['message', 'ticket__ticket_number']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(TicketActivity)
class TicketActivityAdmin(admin.ModelAdmin):
    list_display = ['ticket', 'activity_type', 'performed_by', 'created_at', 'company']
    list_filter = ['activity_type', 'created_at', 'company']
    search_fields = ['description', 'ticket__ticket_number']
    readonly_fields = ['created_at']


@admin.register(KnowledgeBaseArticle)
class KnowledgeBaseArticleAdmin(admin.ModelAdmin):
    list_display = ['title', 'category', 'status', 'author', 'view_count', 
                   'published_at', 'is_featured', 'company']
    list_filter = ['category', 'status', 'is_featured', 'is_pinned', 'published_at', 'company']
    search_fields = ['title', 'content', 'excerpt']
    prepopulated_fields = {'slug': ('title',)}
    readonly_fields = ['view_count', 'helpful_yes', 'helpful_no', 'last_viewed_at']


@admin.register(FAQ)
class FAQAdmin(admin.ModelAdmin):
    list_display = ['question', 'category', 'display_order', 'view_count', 
                   'is_active', 'is_featured', 'company']
    list_filter = ['category', 'is_active', 'is_featured', 'company']
    search_fields = ['question', 'answer']
    list_editable = ['display_order', 'is_active', 'is_featured']


@admin.register(ServiceOutage)
class ServiceOutageAdmin(admin.ModelAdmin):
    list_display = ['title', 'outage_type', 'severity', 'status', 'start_time',
                   'estimated_resolution_time', 'estimated_customers_affected', 'company']
    list_filter = ['outage_type', 'severity', 'status', 'start_time', 'company']
    search_fields = ['title', 'description', 'affected_areas']
    readonly_fields = ['created_at', 'updated_at']