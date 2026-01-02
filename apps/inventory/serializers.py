from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import (
    Supplier, EquipmentType, EquipmentItem, Assignment,
    PurchaseOrder, PurchaseOrderItem, MaintenanceRecord, StockAlert
)
from apps.staff.models import Employee

User = get_user_model()


class SupplierSerializer(serializers.ModelSerializer):
    total_purchases = serializers.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        read_only=True
    )
    equipment_count = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = Supplier
        fields = [
            'id', 'name', 'contact_person', 'email', 'phone',
            'address', 'website', 'tax_id', 'payment_terms',
            'notes', 'is_active', 'total_purchases', 'equipment_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']


class EquipmentTypeSerializer(serializers.ModelSerializer):
    item_count = serializers.IntegerField(read_only=True)
    available_count = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = EquipmentType
        fields = [
            'id', 'name', 'description', 'parent',
            'is_network_equipment', 'has_serial_numbers',
            'requires_assignment', 'item_count', 'available_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']


class EquipmentItemSerializer(serializers.ModelSerializer):
    equipment_type_name = serializers.CharField(
        source='equipment_type.name', 
        read_only=True
    )
    supplier_name = serializers.CharField(
        source='supplier.name', 
        read_only=True
    )
    assigned_to_name = serializers.CharField(
        source='assigned_to.get_full_name', 
        read_only=True
    )
    age_in_months = serializers.IntegerField(read_only=True)
    is_available = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = EquipmentItem
        fields = [
            'id', 'equipment_type', 'equipment_type_name',
            'name', 'model', 'serial_number', 'asset_tag',
            'mac_address', 'supplier', 'supplier_name',
            'purchase_date', 'purchase_price', 'warranty_expiry',
            'status', 'condition', 'location', 'shelf',
            'assigned_to', 'assigned_to_name', 'notes',
            'ip_address', 'firmware_version',
            'age_in_months', 'is_available',
            'qr_code', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at', 'asset_tag']


class AssignmentSerializer(serializers.ModelSerializer):
    equipment_details = EquipmentItemSerializer(
        source='equipment', 
        read_only=True
    )
    assigned_to_details = serializers.SerializerMethodField()
    assigned_by_name = serializers.CharField(
        source='assigned_by.get_full_name', 
        read_only=True
    )
    is_active = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = Assignment
        fields = [
            'id', 'equipment', 'equipment_details',
            'assigned_to', 'assigned_to_details',
            'assigned_by', 'assigned_by_name',
            'assigned_date', 'expected_return_date',
            'actual_return_date', 'condition_on_assignment',
            'condition_on_return', 'purpose', 'notes',
            'is_active', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']
    
    def get_assigned_to_details(self, obj):
        if obj.assigned_to:
            return {
                'id': obj.assigned_to.id,
                'name': obj.assigned_to.get_full_name(),
                'employee_id': obj.assigned_to.employee_id,
                'position': obj.assigned_to.position
            }
        return None
    
    def validate(self, data):
        equipment = data.get('equipment') or self.instance.equipment if self.instance else None
        
        if equipment and equipment.status != 'in_stock':
            raise serializers.ValidationError(
                f"Equipment is not available. Current status: {equipment.status}"
            )
        
        if equipment and equipment.condition in ['faulty', 'poor']:
            raise serializers.ValidationError(
                "Cannot assign equipment in poor or faulty condition"
            )
        
        return data
    
    def create(self, validated_data):
        assignment = super().create(validated_data)
        
        # Update equipment status
        equipment = assignment.equipment
        equipment.status = 'assigned'
        equipment.assigned_to = assignment.assigned_to
        equipment.save()
        
        return assignment


class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    total_price = serializers.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        read_only=True
    )
    pending_quantity = serializers.IntegerField(read_only=True)
    equipment_type_name = serializers.CharField(
        source='equipment_type.name', 
        read_only=True
    )
    
    class Meta:
        model = PurchaseOrderItem
        fields = [
            'id', 'purchase_order', 'equipment_type',
            'equipment_type_name', 'description', 'quantity',
            'unit_price', 'received_quantity', 'total_price',
            'pending_quantity', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']


class PurchaseOrderSerializer(serializers.ModelSerializer):
    items = PurchaseOrderItemSerializer(many=True, read_only=True)
    supplier_name = serializers.CharField(
        source='supplier.name', 
        read_only=True
    )
    prepared_by_name = serializers.CharField(
        source='prepared_by.get_full_name', 
        read_only=True
    )
    approved_by_name = serializers.CharField(
        source='approved_by.get_full_name', 
        read_only=True
    )
    status_display = serializers.CharField(
        source='get_status_display', 
        read_only=True
    )
    
    class Meta:
        model = PurchaseOrder
        fields = [
            'id', 'po_number', 'supplier', 'supplier_name',
            'order_date', 'expected_delivery', 'actual_delivery',
            'status', 'status_display', 'total_amount', 'tax_amount',
            'prepared_by', 'prepared_by_name', 'approved_by',
            'approved_by_name', 'approved_date', 'notes',
            'items', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'created_at', 'updated_at', 'po_number', 
            'total_amount', 'approved_by', 'approved_date'
        ]
    
    def validate(self, data):
        if self.instance and self.instance.status in ['received', 'cancelled']:
            raise serializers.ValidationError(
                "Cannot modify a completed or cancelled purchase order"
            )
        return data


class MaintenanceRecordSerializer(serializers.ModelSerializer):
    equipment_details = EquipmentItemSerializer(
        source='equipment', 
        read_only=True
    )
    performed_by_name = serializers.CharField(
        source='performed_by.get_full_name', 
        read_only=True
    )
    status_display = serializers.CharField(
        source='get_status_display', 
        read_only=True
    )
    
    class Meta:
        model = MaintenanceRecord
        fields = [
            'id', 'equipment', 'equipment_details',
            'scheduled_date', 'completed_date', 'status',
            'status_display', 'maintenance_type', 'description',
            'action_taken', 'cost', 'performed_by',
            'performed_by_name', 'next_maintenance_date',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']


class StockAlertSerializer(serializers.ModelSerializer):
    equipment_type_name = serializers.CharField(
        source='equipment_type.name', 
        read_only=True
    )
    threshold_percentage = serializers.SerializerMethodField()
    
    class Meta:
        model = StockAlert
        fields = [
            'id', 'equipment_type', 'equipment_type_name',
            'threshold', 'current_stock', 'is_active',
            'threshold_percentage', 'triggered_on',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at', 'triggered_on']
    
    def get_threshold_percentage(self, obj):
        if obj.threshold > 0:
            return min(100, int((obj.current_stock / obj.threshold) * 100))
        return 0


class EquipmentReportSerializer(serializers.Serializer):
    equipment_type = serializers.CharField()
    total_count = serializers.IntegerField()
    in_stock = serializers.IntegerField()
    assigned = serializers.IntegerField()
    in_use = serializers.IntegerField()
    under_maintenance = serializers.IntegerField()
    total_value = serializers.DecimalField(max_digits=12, decimal_places=2)


class StockMovementSerializer(serializers.Serializer):
    date = serializers.DateField()
    equipment_type = serializers.CharField()
    movement_type = serializers.CharField()  # in, out, transfer
    quantity = serializers.IntegerField()
    reference = serializers.CharField()
    notes = serializers.CharField()