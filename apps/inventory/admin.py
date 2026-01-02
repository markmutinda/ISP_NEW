from django.contrib import admin
from django.utils.html import format_html
from .models import (
    Supplier, EquipmentType, EquipmentItem, Assignment,
    PurchaseOrder, PurchaseOrderItem, MaintenanceRecord, StockAlert
)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ['name', 'contact_person', 'email', 'phone', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name', 'contact_person', 'email', 'phone']
    list_editable = ['is_active']


@admin.register(EquipmentType)
class EquipmentTypeAdmin(admin.ModelAdmin):
    list_display = ['name', 'is_network_equipment', 'item_count']
    list_filter = ['is_network_equipment', 'has_serial_numbers']
    search_fields = ['name', 'description']
    
    def item_count(self, obj):
        return obj.items.count()
    item_count.short_description = 'Items'


@admin.register(EquipmentItem)
class EquipmentItemAdmin(admin.ModelAdmin):
    list_display = [
        'asset_tag', 'name', 'model', 'serial_number',
        'equipment_type', 'status', 'condition', 'location',
        'assigned_to_display', 'purchase_date'
    ]
    list_filter = [
        'status', 'condition', 'equipment_type',
        'purchase_date', 'supplier'
    ]
    search_fields = [
        'name', 'model', 'serial_number', 'asset_tag',
        'mac_address', 'notes'
    ]
    readonly_fields = ['asset_tag', 'created_display', 'updated_display']  # Use display methods
    fieldsets = (
        ('Basic Information', {
            'fields': (
                'equipment_type', 'name', 'model',
                'serial_number', 'asset_tag', 'mac_address'
            )
        }),
        ('Purchase Information', {
            'fields': (
                'supplier', 'purchase_date', 'purchase_price',
                'warranty_expiry'
            )
        }),
        ('Status & Location', {
            'fields': (
                'status', 'condition', 'location', 'shelf',
                'assigned_to'
            )
        }),
        ('Network Details', {
            'fields': ('ip_address', 'firmware_version'),
            'classes': ('collapse',)
        }),
        ('Additional Information', {
            'fields': ('notes', 'qr_code', 'created_display', 'updated_display'),
            'classes': ('collapse',)
        })
    )
    
    def assigned_to_display(self, obj):
        if obj.assigned_to:
            return obj.assigned_to.get_full_name()
        return '-'
    assigned_to_display.short_description = 'Assigned To'

    def created_display(self, obj):
        return obj.created_at
    created_display.short_description = 'Created At'

    def updated_display(self, obj):
        return obj.updated_at
    updated_display.short_description = 'Updated At'


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = [
        'equipment', 'assigned_to', 'assigned_date',
        'expected_return_date', 'actual_return_date',
        'is_active_display'
    ]
    list_filter = ['assigned_date', 'actual_return_date']
    search_fields = [
        'equipment__name', 'equipment__serial_number',
        'assigned_to__user__first_name', 'assigned_to__user__last_name'
    ]
    readonly_fields = ['created_display', 'updated_display']
    
    def is_active_display(self, obj):
        if obj.is_active():
            return format_html('<span style="color: green;">● Active</span>')
        return format_html('<span style="color: gray;">● Returned</span>')
    is_active_display.short_description = 'Status'

    def created_display(self, obj):
        return obj.created_at
    created_display.short_description = 'Created At'

    def updated_display(self, obj):
        return obj.updated_at
    updated_display.short_description = 'Updated At'


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = [
        'po_number', 'supplier', 'order_date',
        'status', 'total_amount', 'approved_by'
    ]
    list_filter = ['status', 'order_date', 'supplier']
    search_fields = ['po_number', 'supplier__name', 'notes']
    readonly_fields = ['po_number', 'created_display', 'updated_display']
    fieldsets = (
        ('Order Information', {
            'fields': ('po_number', 'supplier', 'order_date')
        }),
        ('Delivery', {
            'fields': ('expected_delivery', 'actual_delivery')
        }),
        ('Financial', {
            'fields': ('total_amount', 'tax_amount')
        }),
        ('Approval', {
            'fields': ('status', 'prepared_by', 'approved_by', 'approved_date')
        }),
        ('Additional', {
            'fields': ('notes', 'created_display', 'updated_display')
        })
    )

    def created_display(self, obj):
        return obj.created_at
    created_display.short_description = 'Created At'

    def updated_display(self, obj):
        return obj.updated_at
    updated_display.short_description = 'Updated At'


@admin.register(PurchaseOrderItem)
class PurchaseOrderItemAdmin(admin.ModelAdmin):
    list_display = [
        'purchase_order', 'equipment_type',
        'description', 'quantity', 'unit_price',
        'total_price', 'received_quantity'
    ]
    list_filter = ['purchase_order__supplier']
    search_fields = ['description', 'equipment_type__name']


@admin.register(MaintenanceRecord)
class MaintenanceRecordAdmin(admin.ModelAdmin):
    list_display = [
        'equipment', 'maintenance_type', 'scheduled_date',
        'completed_date', 'status', 'cost', 'performed_by_display'
    ]
    list_filter = ['status', 'maintenance_type', 'scheduled_date']
    search_fields = ['equipment__name', 'description', 'action_taken']
    
    def performed_by_display(self, obj):
        if obj.performed_by:
            return obj.performed_by.get_full_name()
        return '-'
    performed_by_display.short_description = 'Performed By'


@admin.register(StockAlert)
class StockAlertAdmin(admin.ModelAdmin):
    list_display = [
        'equipment_type', 'threshold', 'current_stock',
        'is_active', 'triggered_on'
    ]
    list_filter = ['is_active', 'triggered_on']
    readonly_fields = ['triggered_on', 'created_display', 'updated_display']

    def created_display(self, obj):
        return obj.created_at
    created_display.short_description = 'Created At'

    def updated_display(self, obj):
        return obj.updated_at
    updated_display.short_description = 'Updated At'