from django.utils.text import slugify
from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import (
    Supplier, EquipmentType, EquipmentItem, Assignment,
    PurchaseOrder, PurchaseOrderItem, MaintenanceRecord, StockAlert
)

User = get_user_model()


class SupplierSerializer(serializers.ModelSerializer):
    total_purchases = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True, default=0
    )
    equipment_count = serializers.IntegerField(read_only=True, default=0)
    # Alias so frontend can use contact_name OR contact_person
    contact_name = serializers.CharField(source='contact_person', read_only=True)

    class Meta:
        model = Supplier
        fields = [
            'id', 'name', 'contact_person', 'contact_name', 'email', 'phone',
            'address', 'website', 'tax_id', 'payment_terms', 'notes',
            'is_active', 'total_purchases', 'equipment_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class EquipmentTypeSerializer(serializers.ModelSerializer):
    item_count = serializers.IntegerField(read_only=True, default=0)
    available_count = serializers.IntegerField(read_only=True, default=0)
    parent_name = serializers.CharField(source='parent.name', read_only=True)

    class Meta:
        model = EquipmentType
        fields = [
            'id', 'name', 'code', 'description', 'parent', 'parent_name',
            'is_network_equipment', 'has_serial_numbers', 'requires_assignment',
            'min_stock_level', 'is_active', 'item_count', 'available_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class EquipmentItemSerializer(serializers.ModelSerializer):
    equipment_type_name = serializers.CharField(source='equipment_type.name', read_only=True)
    equipment_type_text = serializers.CharField(write_only=True, required=False, allow_blank=True)
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    assigned_to_name = serializers.SerializerMethodField()
    assigned_to_customer_name = serializers.SerializerMethodField()
    age_in_months = serializers.SerializerMethodField()
    is_available = serializers.SerializerMethodField()

    class Meta:
        model = EquipmentItem
        fields = [
            'id', 'equipment_type', 'equipment_type_name', 'equipment_type_text',
            'name', 'model', 'serial_number', 'asset_tag', 'mac_address',
            'supplier', 'supplier_name',
            'purchase_date', 'purchase_price', 'warranty_expiry',
            'status', 'condition', 'location', 'shelf',
            'assigned_to', 'assigned_to_name',
            'assigned_to_customer', 'assigned_to_customer_name',
            'notes', 'ip_address', 'firmware_version',
            'age_in_months', 'is_available',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['asset_tag', 'created_at', 'updated_at']
        extra_kwargs = {
            'equipment_type': {'required': False},
        }

    def _next_type_code(self, name):
        base = slugify(name).replace('-', '').upper()[:12] or 'EQP'
        code = base
        suffix = 1
        while EquipmentType.objects.filter(code=code).exists():
            suffix += 1
            code = f"{base[: max(1, 12 - len(str(suffix)))]}{suffix}"
        return code

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if not attrs.get('equipment_type') and not attrs.get('equipment_type_text'):
            raise serializers.ValidationError({
                'equipment_type_text': 'Equipment type is required.'
            })
        return attrs

    def create(self, validated_data):
        type_text = (validated_data.pop('equipment_type_text', '') or '').strip()
        if type_text and not validated_data.get('equipment_type'):
            equipment_type = EquipmentType.objects.filter(name__iexact=type_text, is_active=True).first()
            if not equipment_type:
                equipment_type = EquipmentType.objects.create(
                    name=type_text,
                    code=self._next_type_code(type_text),
                )
            validated_data['equipment_type'] = equipment_type
        return super().create(validated_data)

    def update(self, instance, validated_data):
        type_text = (validated_data.pop('equipment_type_text', '') or '').strip()
        if type_text:
            equipment_type = EquipmentType.objects.filter(name__iexact=type_text, is_active=True).first()
            if not equipment_type:
                equipment_type = EquipmentType.objects.create(
                    name=type_text,
                    code=self._next_type_code(type_text),
                )
            validated_data['equipment_type'] = equipment_type
        return super().update(instance, validated_data)

    def get_assigned_to_name(self, obj):
        if obj.assigned_to:
            return obj.assigned_to.get_full_name()
        return None

    def get_assigned_to_customer_name(self, obj):
        if obj.assigned_to_customer:
            return getattr(obj.assigned_to_customer, 'full_name', str(obj.assigned_to_customer))
        return None

    def get_age_in_months(self, obj):
        return obj.age_in_months

    def get_is_available(self, obj):
        return obj.is_available


class AssignmentSerializer(serializers.ModelSerializer):
    # Frontend-expected flat fields
    equipment_name = serializers.SerializerMethodField()
    equipment_serial = serializers.SerializerMethodField()
    employee_id = serializers.SerializerMethodField()
    employee_name = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    # Keep condition field names consistent with frontend types
    condition_at_assignment = serializers.CharField(
        source='condition_on_assignment', required=False
    )
    condition_at_return = serializers.CharField(
        source='condition_on_return', required=False, allow_null=True
    )

    class Meta:
        model = Assignment
        fields = [
            'id', 'equipment', 'equipment_name', 'equipment_serial',
            'assigned_to', 'employee_id', 'employee_name',
            'purpose', 'assigned_date', 'expected_return_date',
            'actual_return_date', 'condition_at_assignment', 'condition_at_return',
            'notes', 'status', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']

    def get_equipment_name(self, obj):
        return obj.equipment.name if obj.equipment else None

    def get_equipment_serial(self, obj):
        if obj.equipment:
            return obj.equipment.serial_number or obj.equipment.asset_tag
        return None

    def get_employee_id(self, obj):
        if obj.assigned_to:
            return getattr(obj.assigned_to, 'employee_id', str(obj.assigned_to.pk))
        return None

    def get_employee_name(self, obj):
        if obj.assigned_to:
            return obj.assigned_to.get_full_name()
        return None

    def get_status(self, obj):
        return obj.status  # computed property on model

    def validate(self, data):
        equipment = data.get('equipment') or (self.instance.equipment if self.instance else None)
        if equipment and not equipment.is_available:
            raise serializers.ValidationError(
                f"Equipment is not available. Current status: {equipment.status}"
            )
        return data

    def create(self, validated_data):
        assignment = super().create(validated_data)
        equipment = assignment.equipment
        equipment.status = 'assigned'
        equipment.assigned_to = assignment.assigned_to
        equipment.save()
        return assignment


class StockAlertSerializer(serializers.ModelSerializer):
    equipment_type_name = serializers.CharField(source='equipment_type.name', read_only=True)
    # Map frontend fields: current_count → current_stock, shortfall, severity
    current_count = serializers.IntegerField(source='current_stock', read_only=True)
    min_stock_level = serializers.IntegerField(source='equipment_type.min_stock_level', read_only=True)
    shortfall = serializers.SerializerMethodField()
    severity = serializers.SerializerMethodField()

    class Meta:
        model = StockAlert
        fields = [
            'id', 'equipment_type', 'equipment_type_name',
            'threshold', 'current_stock', 'current_count', 'min_stock_level',
            'shortfall', 'severity', 'is_active', 'triggered_on',
        ]

    def get_shortfall(self, obj):
        min_level = obj.equipment_type.min_stock_level
        return max(0, min_level - obj.current_stock)

    def get_severity(self, obj):
        min_level = obj.equipment_type.min_stock_level
        if obj.current_stock == 0:
            return 'critical'
        ratio = obj.current_stock / max(min_level, 1)
        return 'critical' if ratio < 0.25 else 'warning'


class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    total_price = serializers.SerializerMethodField()
    pending_quantity = serializers.SerializerMethodField()
    equipment_type_name = serializers.CharField(source='equipment_type.name', read_only=True)

    class Meta:
        model = PurchaseOrderItem
        fields = [
            'id', 'purchase_order', 'equipment_type', 'equipment_type_name',
            'description', 'quantity', 'unit_price', 'received_quantity',
            'total_price', 'pending_quantity', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']

    def get_total_price(self, obj):
        return obj.total_price

    def get_pending_quantity(self, obj):
        return obj.pending_quantity


class PurchaseOrderSerializer(serializers.ModelSerializer):
    items = PurchaseOrderItemSerializer(many=True, read_only=True)
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    prepared_by_name = serializers.CharField(
        source='prepared_by.get_full_name', read_only=True
    )
    approved_by_name = serializers.CharField(
        source='approved_by.get_full_name', read_only=True
    )

    class Meta:
        model = PurchaseOrder
        fields = [
            'id', 'po_number', 'supplier', 'supplier_name',
            'order_date', 'expected_delivery', 'actual_delivery',
            'status', 'total_amount', 'tax_amount',
            'prepared_by', 'prepared_by_name',
            'approved_by', 'approved_by_name', 'approved_date',
            'notes', 'items', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at', 'po_number', 'total_amount']


class MaintenanceRecordSerializer(serializers.ModelSerializer):
    performed_by_name = serializers.CharField(
        source='performed_by.get_full_name', read_only=True
    )

    class Meta:
        model = MaintenanceRecord
        fields = [
            'id', 'equipment', 'scheduled_date', 'completed_date', 'status',
            'maintenance_type', 'description', 'action_taken', 'cost',
            'performed_by', 'performed_by_name', 'next_maintenance_date',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']
