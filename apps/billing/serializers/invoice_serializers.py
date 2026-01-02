from rest_framework import serializers
from django.utils import timezone
from decimal import Decimal
from ..models.billing_models import Plan, BillingCycle, Invoice, InvoiceItem
from apps.customers.models import Customer
from apps.core.models import Company, User  # if needed
from customers.serializers import CustomerSerializer


class PlanSerializer(serializers.ModelSerializer):
    """Serializer for Plan model"""
    company_name = serializers.CharField(source='company.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    tax_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    total_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    
    class Meta:
        model = Plan
        fields = [
            'id', 'company', 'company_name', 'name', 'code', 'plan_type', 'description',
            'base_price', 'setup_fee', 'tax_inclusive', 'tax_rate', 'tax_amount', 'total_price',
            'download_speed', 'upload_speed', 'data_limit', 'burst_limit',
            'billing_cycle', 'prorated_billing', 'auto_renew', 'contract_period',
            'early_termination_fee', 'is_active', 'is_public',
            'created_by', 'created_by_name', 'updated_by', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_by', 'updated_by', 'created_at', 'updated_at']


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