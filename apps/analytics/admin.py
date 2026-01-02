from django.contrib import admin
from .models import ReportDefinition, DashboardWidget, AnalyticsCache


@admin.register(ReportDefinition)
class ReportDefinitionAdmin(admin.ModelAdmin):
    list_display = ('name', 'report_type', 'format', 'is_active', 'created_by', 'created_at')
    list_filter = ('report_type', 'format', 'is_active')
    search_fields = ('name', 'description')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'report_type', 'description')
        }),
        ('Configuration', {
            'fields': ('query', 'parameters', 'format')
        }),
        ('Status', {
            'fields': ('is_active', 'created_by', 'created_at', 'updated_at')
        }),
    )


@admin.register(DashboardWidget)
class DashboardWidgetAdmin(admin.ModelAdmin):
    list_display = ('name', 'widget_type', 'chart_type', 'position', 'is_visible')
    list_filter = ('widget_type', 'chart_type', 'is_visible')
    search_fields = ('name', 'data_source')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'widget_type', 'chart_type')
        }),
        ('Configuration', {
            'fields': ('data_source', 'refresh_interval', 'position', 'size', 'config')
        }),
        ('Visibility', {
            'fields': ('is_visible', 'created_at', 'updated_at')
        }),
    )


@admin.register(AnalyticsCache)
class AnalyticsCacheAdmin(admin.ModelAdmin):
    list_display = ('cache_key', 'expires_at', 'created_at')
    list_filter = ('expires_at', 'created_at')
    search_fields = ('cache_key',)
    readonly_fields = ('created_at',)