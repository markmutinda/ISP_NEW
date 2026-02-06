#apps/billing/serializers/invoice_serializers.py
from rest_framework import serializers
from django.utils import timezone
from decimal import Decimal
from ..models.billing_models import Plan, BillingCycle, Invoice, InvoiceItem
from apps.customers.models import Customer
from apps.core.models import Company, User  # if needed
from customers.serializers import CustomerSerializer


class PlanSerializer(serializers.ModelSerializer):
    price = serializers.DecimalField(source='base_price', max_digits=10, decimal_places=2, read_only=True)
    validity_days = serializers.IntegerField(source='duration_days', read_only=True)
    subscriber_count = serializers.IntegerField(read_only=True)
    subscribers_count = serializers.IntegerField(source='subscriber_count', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    # Computed display properties
    validity_display = serializers.CharField(read_only=True)
    speed_display = serializers.CharField(read_only=True)
    total_validity_minutes = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = Plan
        fields = [
            'id', 'name', 'code', 'plan_type', 'description',
            'base_price', 'price', 'setup_fee',
            # Speed & Data
            'download_speed', 'upload_speed', 'speed_unit', 'data_limit',
            # Validity - flexible time-based
            'validity_type', 'duration_days', 'validity_days',
            'validity_hours', 'validity_minutes',
            'validity_display', 'speed_display', 'total_validity_minutes',
            # Session/Connection limits
            'max_sessions', 'session_timeout',
            # Burst Speed (MikroTik)
            'burst_download', 'burst_upload', 'burst_threshold', 'burst_time',
            # Fair Usage Policy
            'fup_limit', 'fup_speed',
            # Status & Visibility
            'is_active', 'is_public', 'is_popular',
            'features', 'subscriber_count', 'subscribers_count',
            'created_by', 'created_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'code', 'subscriber_count', 'created_by',
            'created_at', 'updated_at',
            'validity_display', 'speed_display', 'total_validity_minutes',
        ]
    
    def validate_base_price(self, value):
        """Validate base price is positive"""
        if value <= 0:
            raise serializers.ValidationError("Base price must be greater than zero")
        return value


class PlanCreateSerializer(serializers.ModelSerializer):
    """
    Serializer for creating/updating plans.
    
    Supports all validity types for any plan_type (PPPoE, Hotspot, Static, etc.):
    - MINUTES: Short-term plans (e.g., 30min hotspot, 1hr PPPoE)
    - HOURS:   Hourly plans (e.g., 3hr, 6hr, 12hr, 24hr)
    - DAYS:    Daily/weekly/monthly plans (e.g., 1 day, 7 days, 30 days)
    - UNLIMITED: No expiration
    """
    
    class Meta:
        model = Plan
        fields = [
            'name', 'plan_type', 'description',
            'base_price', 'setup_fee',
            # Speed & Data
            'download_speed', 'upload_speed', 'speed_unit', 'data_limit',
            # Validity - flexible time-based
            'validity_type', 'duration_days', 'validity_hours', 'validity_minutes',
            # Session/Connection limits
            'max_sessions', 'session_timeout',
            # Burst Speed (MikroTik)
            'burst_download', 'burst_upload', 'burst_threshold', 'burst_time',
            # Fair Usage Policy
            'fup_limit', 'fup_speed',
            # Status & Visibility
            'is_active', 'is_public', 'is_popular',
            'features',
        ]
    
    def validate(self, data):
        """Validate plan data with cross-field validity checks."""
        # Validate download/upload speeds
        if data.get('download_speed') and data['download_speed'] <= 0:
            raise serializers.ValidationError({"download_speed": "Download speed must be greater than zero"})
        
        if data.get('upload_speed') and data['upload_speed'] <= 0:
            raise serializers.ValidationError({"upload_speed": "Upload speed must be greater than zero"})
        
        # Validate data limit if provided
        if data.get('data_limit') and data['data_limit'] <= 0:
            raise serializers.ValidationError({"data_limit": "Data limit must be greater than zero"})
        
        # Validate FUP values if provided
        if data.get('fup_limit') and data['fup_limit'] <= 0:
            raise serializers.ValidationError({"fup_limit": "FUP limit must be greater than zero"})
        
        if data.get('fup_speed') and data['fup_speed'] <= 0:
            raise serializers.ValidationError({"fup_speed": "FUP speed must be greater than zero"})
        
        # Cross-validate validity_type with corresponding duration field
        validity_type = data.get('validity_type', self.instance.validity_type if self.instance else 'DAYS')
        
        if validity_type == 'MINUTES':
            minutes = data.get('validity_minutes')
            if minutes is not None and minutes <= 0:
                raise serializers.ValidationError(
                    {"validity_minutes": "Duration in minutes must be greater than zero"}
                )
        elif validity_type == 'HOURS':
            hours = data.get('validity_hours')
            if hours is not None and hours <= 0:
                raise serializers.ValidationError(
                    {"validity_hours": "Duration in hours must be greater than zero"}
                )
        elif validity_type == 'DAYS':
            days = data.get('duration_days')
            if days is not None and days <= 0:
                raise serializers.ValidationError(
                    {"duration_days": "Duration in days must be greater than zero"}
                )
        # UNLIMITED needs no duration validation
        
        return data


class BillingCycleSerializer(serializers.ModelSerializer):
    """Serializer for BillingCycle model"""
    company_name = serializers.CharField(source='company.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    closed_by_name = serializers.CharField(source='closed_by.get_full_name', read_only=True)
    
    class Meta:
        model = BillingCycle
        fields = [
            'id', 'company', 'company_name', 'name', 'cycle_code', 'start_date', 'end_date',
            'due_date', 'status', 'is_locked', 'total_invoices', 'total_amount',
            'total_paid', 'total_outstanding', 'closed_by', 'closed_by_name',
            'closed_at', 'notes', 'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'cycle_code', 'total_invoices', 'total_amount', 'total_paid',
            'total_outstanding', 'closed_by', 'closed_at', 'created_by',
            'created_at', 'updated_at'
        ]


class InvoiceItemSerializer(serializers.ModelSerializer):
    """Serializer for InvoiceItem model"""
    
    class Meta:
        model = InvoiceItem
        fields = [
            'id', 'invoice', 'description', 'quantity', 'unit_price', 'tax_rate',
            'tax_amount', 'total', 'service_type', 'service_period_start',
            'service_period_end', 'created_at', 'updated_at'
        ]
        read_only_fields = ['tax_amount', 'total', 'created_at', 'updated_at']


class InvoiceSerializer(serializers.ModelSerializer):
    """Serializer for Invoice model"""
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True)
    company_name = serializers.CharField(source='company.name', read_only=True)
    billing_cycle_code = serializers.CharField(source='billing_cycle.cycle_code', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    issued_by_name = serializers.CharField(source='issued_by.get_full_name', read_only=True)
    paid_by_name = serializers.CharField(source='paid_by.get_full_name', read_only=True)
    items = InvoiceItemSerializer(many=True, read_only=True)
    
    class Meta:
        model = Invoice
        fields = [
            'id', 'invoice_number', 'company', 'company_name', 'customer', 'customer_name',
            'customer_code', 'billing_cycle', 'billing_cycle_code', 'billing_date',
            'due_date', 'payment_terms', 'service_period_start', 'service_period_end',
            'subtotal', 'tax_amount', 'discount_amount', 'total_amount', 'amount_paid',
            'balance', 'status', 'is_overdue', 'overdue_days', 'paid_at', 'paid_by',
            'paid_by_name', 'service_connection', 'plan', 'notes', 'internal_notes',
            'created_by', 'created_by_name', 'issued_by', 'issued_by_name', 'issued_at',
            'created_at', 'updated_at', 'items'
        ]
        read_only_fields = [
            'invoice_number', 'subtotal', 'tax_amount', 'total_amount', 'balance',
            'is_overdue', 'overdue_days', 'paid_at', 'issued_by', 'issued_at',
            'created_by', 'created_at', 'updated_at'
        ]


class InvoiceCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating invoices"""
    items = InvoiceItemSerializer(many=True, required=False)
    
    class Meta:
        model = Invoice
        fields = [
            'customer', 'billing_cycle', 'billing_date', 'due_date', 'payment_terms',
            'service_period_start', 'service_period_end', 'discount_amount',
            'service_connection', 'plan', 'notes', 'internal_notes', 'items'
        ]
    
    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        invoice = Invoice.objects.create(**validated_data)
        
        for item_data in items_data:
            InvoiceItem.objects.create(invoice=invoice, **item_data)
        
        # Calculate totals
        from ..calculators.invoice_calculator import InvoiceCalculator
        totals = InvoiceCalculator.calculate_invoice_totals(invoice)
        
        invoice.subtotal = totals['subtotal']
        invoice.tax_amount = totals['tax_amount']
        invoice.total_amount = totals['total_amount']
        invoice.balance = totals['total_amount']
        invoice.save()
        
        return invoice


class InvoiceDetailSerializer(InvoiceSerializer):
    """Detailed serializer for Invoice model"""
    customer_details = CustomerSerializer(source='customer', read_only=True)
    items = InvoiceItemSerializer(many=True, read_only=True)
    
    class Meta(InvoiceSerializer.Meta):
        fields = InvoiceSerializer.Meta.fields + ['customer_details']
