from django.contrib import admin
from .models import (
    RadCheck,
    RadReply,
    RadUserGroup,
    RadGroupCheck,
    RadGroupReply,
    RadAcct,
    Nas,
    RadPostAuth,
    RadiusBandwidthProfile
)


@admin.register(RadCheck)
class RadCheckAdmin(admin.ModelAdmin):
    list_display = ['username', 'attribute', 'op', 'value', 'customer']
    list_filter = ['attribute', 'op']
    search_fields = ['username', 'value']
    raw_id_fields = ['customer']


@admin.register(RadReply)
class RadReplyAdmin(admin.ModelAdmin):
    list_display = ['username', 'attribute', 'op', 'value', 'customer']
    list_filter = ['attribute', 'op']
    search_fields = ['username', 'value']
    raw_id_fields = ['customer']


@admin.register(RadUserGroup)
class RadUserGroupAdmin(admin.ModelAdmin):
    list_display = ['username', 'groupname', 'priority']
    list_filter = ['groupname']
    search_fields = ['username', 'groupname']


@admin.register(RadGroupCheck)
class RadGroupCheckAdmin(admin.ModelAdmin):
    list_display = ['groupname', 'attribute', 'op', 'value']
    list_filter = ['groupname', 'attribute']
    search_fields = ['groupname']


@admin.register(RadGroupReply)
class RadGroupReplyAdmin(admin.ModelAdmin):
    list_display = ['groupname', 'attribute', 'op', 'value']
    list_filter = ['groupname', 'attribute']
    search_fields = ['groupname']


@admin.register(RadAcct)
class RadAcctAdmin(admin.ModelAdmin):
    list_display = [
        'username', 'nasipaddress', 'framedipaddress',
        'acctstarttime', 'acctstoptime', 'acctsessiontime',
        'acctinputoctets', 'acctoutputoctets'
    ]
    list_filter = ['nasipaddress', 'acctstarttime', 'acctterminatecause']
    search_fields = ['username', 'callingstationid', 'framedipaddress']
    readonly_fields = [
        'radacctid', 'acctsessionid', 'acctuniqueid', 'username',
        'nasipaddress', 'nasportid', 'nasporttype',
        'acctstarttime', 'acctupdatetime', 'acctstoptime',
        'acctinterval', 'acctsessiontime', 'acctauthentic',
        'connectinfo_start', 'connectinfo_stop',
        'acctinputoctets', 'acctoutputoctets',
        'calledstationid', 'callingstationid', 'acctterminatecause',
        'servicetype', 'framedprotocol', 'framedipaddress'
    ]
    raw_id_fields = ['customer', 'router']
    date_hierarchy = 'acctstarttime'


@admin.register(Nas)
class NasAdmin(admin.ModelAdmin):
    list_display = ['nasname', 'shortname', 'type', 'secret', 'router', 'created_at']
    list_filter = ['type', 'created_at']
    search_fields = ['nasname', 'shortname', 'description']
    raw_id_fields = ['router']


@admin.register(RadPostAuth)
class RadPostAuthAdmin(admin.ModelAdmin):
    list_display = ['username', 'reply', 'nasipaddress', 'callingstationid', 'authdate']
    list_filter = ['reply', 'authdate']
    search_fields = ['username', 'callingstationid']
    readonly_fields = ['id', 'username', 'reply', 'authdate', 'nasipaddress', 'callingstationid']
    date_hierarchy = 'authdate'


@admin.register(RadiusBandwidthProfile)
class RadiusBandwidthProfileAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'download_speed', 'upload_speed',
        'simultaneous_use', 'is_active', 'created_at'
    ]
    list_filter = ['is_active', 'created_at']
    search_fields = ['name', 'description']
    readonly_fields = ['id', 'created_at', 'updated_at', 'mikrotik_rate_limit']
    
    fieldsets = (
        ('Basic Info', {
            'fields': ('name', 'description', 'is_active')
        }),
        ('Bandwidth', {
            'fields': ('download_speed', 'upload_speed', 'priority')
        }),
        ('Burst Settings', {
            'fields': ('burst_download', 'burst_upload', 'burst_threshold', 'burst_time'),
            'classes': ('collapse',)
        }),
        ('Data Limits', {
            'fields': ('daily_limit_mb', 'monthly_limit_mb'),
            'classes': ('collapse',)
        }),
        ('Session Limits', {
            'fields': ('session_timeout', 'idle_timeout', 'simultaneous_use')
        }),
        ('Generated', {
            'fields': ('mikrotik_rate_limit',),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('id', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
