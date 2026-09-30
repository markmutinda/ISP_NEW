from django.contrib import admin

from .models import CPEDevice, CPETaskLog


@admin.register(CPEDevice)
class CPEDeviceAdmin(admin.ModelAdmin):
    list_display = ['serial_number', 'label', 'status', 'manufacturer', 'model_name', 'last_inform_at']
    list_filter = ['status', 'manufacturer']
    search_fields = ['serial_number', 'label', 'genieacs_device_id']
    readonly_fields = ['acs_username', 'acs_password', 'created_at', 'updated_at']


@admin.register(CPETaskLog)
class CPETaskLogAdmin(admin.ModelAdmin):
    list_display = ['device', 'task_type', 'status', 'created_at']
    list_filter = ['task_type', 'status']
    readonly_fields = ['created_at', 'updated_at']