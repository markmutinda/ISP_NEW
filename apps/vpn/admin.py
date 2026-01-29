from django.contrib import admin
from .models import (
    CertificateAuthority,
    VPNCertificate,
    VPNConnection,
    VPNServer,
    VPNConnectionLog
)


@admin.register(CertificateAuthority)
class CertificateAuthorityAdmin(admin.ModelAdmin):
    list_display = ['name', 'common_name', 'is_active', 'valid_from', 'valid_until', 'created_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'common_name']
    readonly_fields = ['created_at', 'updated_at', 'valid_from', 'valid_until']
    
    fieldsets = (
        ('Basic Info', {
            'fields': ('name', 'common_name', 'organization', 'country', 'is_active')
        }),
        ('Validity', {
            'fields': ('valid_from', 'valid_until', 'validity_days')
        }),
        ('Certificates', {
            'fields': ('ca_certificate', 'ca_private_key', 'dh_parameters', 'tls_auth_key'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(VPNCertificate)
class VPNCertificateAdmin(admin.ModelAdmin):
    list_display = ['common_name', 'certificate_type', 'status', 'router', 'valid_from', 'valid_until', 'created_at']
    list_filter = ['certificate_type', 'status', 'created_at']
    search_fields = ['common_name', 'router__name']
    readonly_fields = ['created_at', 'updated_at', 'valid_from', 'valid_until', 'serial_number']
    raw_id_fields = ['router', 'ca']
    
    fieldsets = (
        ('Basic Info', {
            'fields': ('common_name', 'certificate_type', 'status', 'router', 'ca')
        }),
        ('Validity', {
            'fields': ('valid_from', 'valid_until', 'serial_number')
        }),
        ('Certificate Data', {
            'fields': ('certificate', 'private_key', 'certificate_request'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(VPNServer)
class VPNServerAdmin(admin.ModelAdmin):
    list_display = ['name', 'server_address', 'port', 'protocol', 'status', 'max_clients', 'connected_clients']
    list_filter = ['status', 'protocol', 'created_at']
    search_fields = ['name', 'server_address']
    readonly_fields = ['created_at', 'updated_at', 'connected_clients']


@admin.register(VPNConnection)
class VPNConnectionAdmin(admin.ModelAdmin):
    list_display = ['router', 'vpn_ip', 'status', 'connected_at', 'disconnected_at', 'bytes_sent', 'bytes_received']
    list_filter = ['status', 'connected_at']
    search_fields = ['router__name', 'vpn_ip', 'real_ip']
    readonly_fields = ['connected_at', 'disconnected_at', 'bytes_sent', 'bytes_received']
    raw_id_fields = ['router', 'certificate', 'server']


@admin.register(VPNConnectionLog)
class VPNConnectionLogAdmin(admin.ModelAdmin):
    list_display = ['router', 'event_type', 'vpn_ip', 'real_ip', 'created_at']
    list_filter = ['event_type', 'created_at']
    search_fields = ['router__name', 'vpn_ip', 'real_ip']
    readonly_fields = ['created_at']
    raw_id_fields = ['router', 'connection']
