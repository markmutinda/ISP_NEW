# apps/billing/serializers/voucher_serializers.py
from rest_framework import serializers
from django.utils import timezone
from decimal import Decimal
# from ..models.voucher_models import VoucherBatch, Voucher, VoucherUsage   # ← COMMENTED OUT to prevent early loading / circular import issues

from customers.serializers import CustomerSerializer


class VoucherBatchSerializer(serializers.ModelSerializer):
    """Serializer for VoucherBatch model"""
    company_name = serializers.CharField(source='company.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    approved_by_name = serializers.CharField(source='approved_by.get_full_name', read_only=True)
    
    class Meta:
        model = 'VoucherBatch'  # ← Changed to string literal (safe)
        fields = [
            'id', 'batch_number', 'company', 'company_name', 'name', 'description',
            'voucher_type', 'face_value', 'sale_price', 'valid_from', 'valid_to',
            'is_reusable', 'max_uses', 'quantity', 'issued_count', 'used_count',
            'available_count', 'status', 'is_active', 'prefix', 'length', 'charset',
            'minimum_purchase', 'customer_restriction', 'plan_restriction',
            'created_by', 'created_by_name', 'approved_by', 'approved_by_name',
            'approved_at', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'batch_number', 'issued_count', 'used_count', 'available_count',
            'approved_by', 'approved_at', 'created_by', 'created_at', 'updated_at'
        ]


class VoucherBatchCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating voucher batches"""
    
    class Meta:
        model = 'VoucherBatch'  # ← Changed to string literal
        fields = [
            'name', 'description', 'voucher_type', 'face_value', 'sale_price',
            'valid_from', 'valid_to', 'is_reusable', 'max_uses', 'quantity',
            'is_active', 'prefix', 'length', 'charset', 'minimum_purchase',
            'customer_restriction', 'plan_restriction'
        ]
    
    def validate(self, data):
        # Validate dates
        valid_from = data.get('valid_from')
        valid_to = data.get('valid_to')
        
        if valid_from and valid_to and valid_to <= valid_from:
            raise serializers.ValidationError("Valid to date must be after valid from date")
        
        # Validate values
        face_value = data.get('face_value')
        sale_price = data.get('sale_price')
        
        if face_value and face_value <= 0:
            raise serializers.ValidationError("Face value must be greater than zero")
        
        if sale_price and sale_price <= 0:
            raise serializers.ValidationError("Sale price must be greater than zero")
        
        if face_value and sale_price and sale_price > face_value:
            raise serializers.ValidationError("Sale price cannot be greater than face value")
        
        return data


class VoucherSerializer(serializers.ModelSerializer):
    """Serializer for Voucher model"""
    batch_name = serializers.CharField(source='batch.name', read_only=True)
    batch_number = serializers.CharField(source='batch.batch_number', read_only=True)
    voucher_type = serializers.CharField(source='batch.voucher_type', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    sold_by_name = serializers.CharField(source='sold_by.get_full_name', read_only=True)
    sold_to_name = serializers.CharField(source='sold_to.full_name', read_only=True)
    sold_to_code = serializers.CharField(source='sold_to.customer_code', read_only=True)
    is_valid = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = 'Voucher'  # ← Changed to string literal
        fields = [
            'id', 'batch', 'batch_name', 'batch_number', 'voucher_type', 'code', 'pin',
            'face_value', 'sale_price', 'remaining_value', 'valid_from', 'valid_to',
            'is_reusable', 'max_uses', 'use_count', 'status', 'sold_to', 'sold_to_name',
            'sold_to_code', 'sold_at', 'sold_by', 'sold_by_name', 'created_by',
            'created_by_name', 'created_at', 'updated_at', 'is_valid'
        ]
        read_only_fields = [
            'code', 'pin', 'remaining_value', 'use_count', 'sold_at', 'sold_by',
            'created_by', 'created_at', 'updated_at', 'is_valid'
        ]


class VoucherCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating individual vouchers"""
    
    class Meta:
        model = 'Voucher'  # ← Changed to string literal
        fields = [
            'batch', 'code', 'pin', 'face_value', 'sale_price', 'valid_from',
            'valid_to', 'is_reusable', 'max_uses'
        ]
    
    def validate(self, data):
        # Validate dates against batch
        batch = data.get('batch')
        valid_from = data.get('valid_from')
        valid_to = data.get('valid_to')
        
        if batch:
            if valid_from and valid_from < batch.valid_from:
                raise serializers.ValidationError(
                    "Voucher valid from cannot be before batch valid from"
                )
            
            if valid_to and valid_to > batch.valid_to:
                raise serializers.ValidationError(
                    "Voucher valid to cannot be after batch valid to"
                )
        
        # Validate values
        face_value = data.get('face_value')
        sale_price = data.get('sale_price')
        
        if face_value and face_value <= 0:
            raise serializers.ValidationError("Face value must be greater than zero")
        
        if sale_price and sale_price <= 0:
            raise serializers.ValidationError("Sale price must be greater than zero")
        
        if face_value and sale_price and sale_price > face_value:
            raise serializers.ValidationError("Sale price cannot be greater than face value")
        
        return data


class VoucherRedeemSerializer(serializers.Serializer):
    """Serializer for redeeming vouchers"""
    customer_id = serializers.IntegerField(required=True)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=True)
    description = serializers.CharField(required=False, allow_blank=True)
    
    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than zero")
        return value


class VoucherUsageSerializer(serializers.ModelSerializer):
    """Serializer for VoucherUsage model"""
    voucher_code = serializers.CharField(source='voucher.code', read_only=True)
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    payment_number = serializers.CharField(source='payment.payment_number', read_only=True)
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)
    
    class Meta:
        model = 'VoucherUsage'  # ← Changed to string literal
        fields = [
            'id', 'voucher', 'voucher_code', 'customer', 'customer_name',
            'customer_code', 'amount', 'remaining_balance', 'description',
            'payment', 'payment_number', 'invoice', 'invoice_number',
            'created_by', 'created_by_name', 'created_at'
        ]
        read_only_fields = ['created_by', 'created_at']
