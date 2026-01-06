# apps/network/admin.py

from django.contrib import admin
from django.utils.html import format_html

from .models import (
    # NEW: Router Models
    Router,
    RouterEvent,
    MikrotikInterface,
    HotspotUser,
    PPPoEUser,
    MikrotikQueue,

    # OLT Models
    OLTDevice, OLTPort, PONPort, ONUDevice, OLTConfig,
    
    # TR-069 Models
    ACSConfiguration, CPEDevice, TR069Parameter, TR069Session,
    
    # IPAM Models
    Subnet, VLAN, IPPool, IPAddress, DHCPRange,
)


# ================================
#       ROUTER ADMIN SECTION
# ================================

@admin.register(Router)
class RouterAdmin(admin.ModelAdmin):
    list_display = ('name', 'ip_address', 'router_type', 'status', 'last_seen', 'active_users', 'is_authenticated', 'company')
    list_filter = ('router_type', 'status', 'is_active', 'is_authenticated', 'company')
    search_fields = ('name', 'ip_address', 'model', 'location', 'auth_key')
    readonly_fields = ('auth_key', 'authenticated_at', 'last_seen', 'created_at', 'updated_at')
    fieldsets = (
        (None, {
            'fields': ('company', 'name', 'router_type', 'model', 'firmware_version', 'location', ('latitude', 'longitude'))
        }),
        ('Connection & Auth', {
            'fields': ('ip_address', 'mac_address', 'api_port', 'api_username', 'api_password')
        }),
        ('Status & Metrics', {
            'fields': ('status', 'is_active', 'last_seen', 'uptime', 'uptime_percentage', 'sla_target', ('total_users', 'active_users'))
        }),
        ('Self-Registration', {
            'fields': ('auth_key', 'is_authenticated', 'authenticated_at'),
            'description': 'Used for router self-registration via script.'
        }),
        ('Extra', {
            'fields': ('tags', 'notes'),
            'classes': ('collapse',)
        }),
    )


@admin.register(RouterEvent)
class RouterEventAdmin(admin.ModelAdmin):
    list_display = ('router', 'event_type', 'message_truncated', 'created_at')
    list_filter = ('event_type', 'router__company')
    search_fields = ('router__name', 'message', 'event_type')
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'

    def message_truncated(self, obj):
        return obj.message[:75] + ('...' if len(obj.message) > 75 else '')
    message_truncated.short_description = 'Message'


# Mikrotik Sub-models (now linked to Router)
@admin.register(MikrotikInterface)
class MikrotikInterfaceAdmin(admin.ModelAdmin):
    list_display = ('interface_name', 'router', 'interface_type', 'admin_state', 'operational_state', 'rx_bytes', 'tx_bytes')
    list_filter = ('interface_type', 'admin_state', 'operational_state', 'router__company')
    search_fields = ('interface_name', 'router__name')


@admin.register(HotspotUser)
class HotspotUserAdmin(admin.ModelAdmin):
    list_display = ('username', 'router', 'status', 'ip_address', 'bytes_in', 'bytes_out', 'customer')
    list_filter = ('status', 'profile', 'router__company')
    search_fields = ('username', 'mac_address', 'service_connection__customer__full_name', 'router__name')
    
    def customer(self, obj):
        if obj.service_connection and obj.service_connection.customer:
            return obj.service_connection.customer.full_name
        return 'Unassigned'
    customer.short_description = 'Customer'


@admin.register(PPPoEUser)
class PPPoEUserAdmin(admin.ModelAdmin):
    list_display = ('username', 'router', 'status', 'local_address', 'remote_address', 'customer')
    list_filter = ('status', 'profile', 'router__company')
    search_fields = ('username', 'caller_id', 'service_connection__customer__full_name', 'router__name')
    
    def customer(self, obj):
        if obj.service_connection and obj.service_connection.customer:
            return obj.service_connection.customer.full_name
        return 'Unassigned'
    customer.short_description = 'Customer'


@admin.register(MikrotikQueue)
class MikrotikQueueAdmin(admin.ModelAdmin):
    list_display = ('queue_name', 'router', 'queue_type', 'target', 'max_limit', 'disabled')
    list_filter = ('queue_type', 'disabled', 'router__company')
    search_fields = ('queue_name', 'target', 'router__name')


# ================================
#          OLT ADMIN SECTION
# ================================

@admin.register(OLTDevice)
class OLTDeviceAdmin(admin.ModelAdmin):
    list_display = ('name', 'vendor', 'ip_address', 'status', 'last_sync', 'company')
    list_filter = ('vendor', 'status', 'company')
    search_fields = ('name', 'hostname', 'ip_address', 'serial_number')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(OLTPort)
class OLTPortAdmin(admin.ModelAdmin):
    list_display = ('port_number', 'olt', 'port_type', 'admin_state', 'operational_state')
    list_filter = ('port_type', 'admin_state', 'operational_state')
    search_fields = ('port_number', 'olt__name')


@admin.register(PONPort)
class PONPortAdmin(admin.ModelAdmin):
    list_display = ('pon_index', 'olt_port', 'pon_type', 'status', 'registered_onus', 'total_onus')
    list_filter = ('pon_type', 'status')
    search_fields = ('pon_index', 'olt_port__port_number')


@admin.register(ONUDevice)
class ONUDeviceAdmin(admin.ModelAdmin):
    list_display = ('serial_number_short', 'pon_port', 'status', 'rx_power', 'tx_power', 'customer')
    list_filter = ('status', 'onu_type', 'pon_port')
    search_fields = ('serial_number', 'mac_address', 'service_connection__customer__full_name')
    
    def serial_number_short(self, obj):
        return obj.serial_number[:8] + '...' if len(obj.serial_number) > 8 else obj.serial_number
    serial_number_short.short_description = 'Serial'
    
    def customer(self, obj):
        if obj.service_connection and obj.service_connection.customer:
            return obj.service_connection.customer.full_name
        return 'Unassigned'
    customer.short_description = 'Customer'


@admin.register(OLTConfig)
class OLTConfigAdmin(admin.ModelAdmin):
    list_display = ('olt', 'config_type', 'version', 'is_active', 'applied_by', 'applied_date')
    list_filter = ('config_type', 'is_active', 'olt')
    search_fields = ('olt__name', 'version')


# ================================
#        TR-069 ADMIN SECTION
# ================================

@admin.register(ACSConfiguration)
class ACSConfigurationAdmin(admin.ModelAdmin):
    list_display = ('name', 'acs_url', 'periodic_interval', 'is_active', 'company')
    list_filter = ('is_active', 'company')
    search_fields = ('name', 'acs_url')


@admin.register(CPEDevice)
class CPEDeviceAdmin(admin.ModelAdmin):
    list_display = ('serial_number_short', 'manufacturer', 'model', 'connection_status', 'provisioned', 'customer')
    list_filter = ('manufacturer', 'connection_status', 'provisioned', 'company')
    search_fields = ('serial_number', 'model', 'cpe_id', 'service_connection__customer__full_name')
    
    def serial_number_short(self, obj):
        return obj.serial_number[:8] + '...' if len(obj.serial_number) > 8 else obj.serial_number
    serial_number_short.short_description = 'Serial'
    
    def customer(self, obj):
        if obj.service_connection and obj.service_connection.customer:
            return obj.service_connection.customer.full_name
        return 'Unassigned'
    customer.short_description = 'Customer'


@admin.register(TR069Parameter)
class TR069ParameterAdmin(admin.ModelAdmin):
    list_display = ('parameter_short', 'cpe_device', 'parameter_type', 'access_type', 'current_value')
    list_filter = ('parameter_type', 'access_type', 'cpe_device')
    search_fields = ('parameter_name', 'current_value')
    
    def parameter_short(self, obj):
        return obj.parameter_name[:50] + '...' if len(obj.parameter_name) > 50 else obj.parameter_name
    parameter_short.short_description = 'Parameter'


@admin.register(TR069Session)
class TR069SessionAdmin(admin.ModelAdmin):
    list_display = ('session_id_short', 'cpe_device', 'session_type', 'status', 'start_time', 'duration')
    list_filter = ('session_type', 'status')
    search_fields = ('session_id', 'cpe_device__serial_number')
    
    def session_id_short(self, obj):
        return obj.session_id[:20] + '...' if len(obj.session_id) > 20 else obj.session_id
    session_id_short.short_description = 'Session ID'


# ================================
#          IPAM ADMIN SECTION
# ================================

@admin.register(Subnet)
class SubnetAdmin(admin.ModelAdmin):
    list_display = ('name', 'network_cidr', 'version', 'total_ips', 'used_ips', 'utilization', 'company')
    list_filter = ('version', 'is_public', 'company')
    search_fields = ('name', 'network_address', 'description')
    
    def network_cidr(self, obj):
        return f"{obj.network_address}/{obj.cidr}"
    network_cidr.short_description = 'Network'
    
    def utilization(self, obj):
        return f"{obj.utilization_percentage:.1f}%"
    utilization.short_description = 'Utilization'


@admin.register(VLAN)
class VLANAdmin(admin.ModelAdmin):
    list_display = ('vlan_id', 'name', 'company', 'subnet')
    list_filter = ('company',)
    search_fields = ('name', 'description')


@admin.register(IPPool)
class IPPoolAdmin(admin.ModelAdmin):
    list_display = ('name', 'subnet', 'pool_type', 'start_ip', 'end_ip', 'is_active', 'utilization')
    list_filter = ('pool_type', 'is_active', 'subnet')
    search_fields = ('name', 'start_ip', 'end_ip')
    
    def utilization(self, obj):
        if obj.total_ips > 0:
            return f"{(obj.used_ips / obj.total_ips) * 100:.1f}%"
        return "0%"
    utilization.short_description = 'Utilization'


@admin.register(IPAddress)
class IPAddressAdmin(admin.ModelAdmin):
    list_display = ('ip_address', 'subnet', 'assignment_type', 'status', 'hostname', 'customer')
    list_filter = ('assignment_type', 'status', 'subnet')
    search_fields = ('ip_address', 'hostname', 'mac_address', 'service_connection__customer__full_name')
    
    def customer(self, obj):
        if obj.service_connection and obj.service_connection.customer:
            return obj.service_connection.customer.full_name
        return ''
    customer.short_description = 'Customer'


@admin.register(DHCPRange)
class DHCPRangeAdmin(admin.ModelAdmin):
    list_display = ('name', 'ip_pool', 'start_ip', 'end_ip', 'lease_time', 'is_active')
    list_filter = ('is_active', 'ip_pool')
    search_fields = ('name', 'start_ip', 'end_ip')