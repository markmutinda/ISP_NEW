from django.contrib import admin
from .models import NetworkMapElement


@admin.register(NetworkMapElement)
class NetworkMapElementAdmin(admin.ModelAdmin):
    list_display = ('name', 'element_type', 'status', 'severity', 'created_at')
    list_filter = ('element_type', 'status', 'severity')
    search_fields = ('name', 'notes')
    