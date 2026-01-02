from rest_framework import serializers
from django.utils import timezone
from decimal import Decimal
from ..models.payment_models import PaymentMethod, Payment, Receipt
from customers.serializers import CustomerSerializer
from ..serializers.invoice_serializers import InvoiceSerializer


class PaymentMethodSerializer(serializers.ModelSerializer):
    """Serializer for PaymentMethod model"""
    company_name = serializers.CharField(source='company.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    updated_by_name = serializers.CharField(source='updated_by.get_full_name', read_only=True)
    
    class Meta:
        model = PaymentMethod
        fields = [
            'id', 'company', 'company_name', 'name', 'code', 'method_type', 'description',
            'is_active', 'requires_confirmation', 'confirmation_timeout',
            'transaction_fee', 'fee_type', 'minimum_amount', 'maximum_amount',
            'integration_class', 'config_json', 'status', 'last_used',
            'created_by', 'created_by_name', 'updated_by', 'updated_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_by', 'updated_by', 'created_at', 'updated_at', 'last_used']


class PaymentSerializer(serializers.ModelSerializer):
    """Serializer for Payment model"""
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True)
    company_name = serializers.CharField(source='company.name', read_only=True)
    payment_method_name = serializers.CharField(source='payment_method.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    processed_by_name = serializers.CharField(source='processed_by.get_full_name', read_only=True)
    reconciled_by_name = serializers.CharField(source='reconciled_by.get_full_name', read_only=True)
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)
    
    class Meta:
        model = Payment
        fields = [
            'id', 'payment_number', 'company', 'company_name', 'customer', 'customer_name',
            'customer_code', 'invoice', 'invoice_number', 'amount', 'transaction_fee',
            'net_amount', 'currency', 'payment_method', 'payment_method_name',
            'payment_reference', 'transaction_id', 'status', 'is_reconciled',
            'payment_date', 'processed_at', 'reconciled_at', 'payer_name',
            'payer_phone', 'payer_email', 'payer_id_number', 'bank_name',
            'account_number', 'branch', 'cheque_number', 'mpesa_receipt',
            'mpesa_phone', 'mpesa_name', 'notes', 'failure_reason',
            'created_by', 'created_by_name', 'processed_by', 'processed_by_name',
            'reconciled_by', 'reconciled_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'payment_number', 'net_amount', 'created_by', 'processed_by',
            'reconciled_by', 'created_at', 'updated_at'
        ]


class PaymentCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating payments"""
    
    class Meta:
        model = Payment
        fields = [
            'customer', 'invoice', 'amount', 'payment_method', 'payment_reference',
            'transaction_id', 'payer_name', 'payer_phone', 'payer_email',
            'payer_id_number', 'bank_name', 'account_number', 'branch',
            'cheque_number', 'mpesa_receipt', 'mpesa_phone', 'mpesa_name', 'notes'
        ]
    
    def validate(self, data):
        # Validate amount
        amount = data.get('amount')
        if amount and amount <= 0:
            raise serializers.ValidationError("Amount must be greater than zero")
        
        # Validate payment method if provided
        payment_method = data.get('payment_method')
        if payment_method:
            if not payment_method.is_active:
                raise serializers.ValidationError("Payment method is not active")
            
            # Check amount limits
            if amount and not payment_method.is_amount_valid(amount):
                raise serializers.ValidationError(
                    f"Amount must be between {payment_method.minimum_amount} "
                    f"and {payment_method.maximum_amount}"
                )
        
        return data


class PaymentDetailSerializer(PaymentSerializer):
    """Detailed serializer for Payment model"""
    customer_details = CustomerSerializer(source='customer', read_only=True)
    invoice_details = InvoiceSerializer(source='invoice', read_only=True)
    
    class Meta(PaymentSerializer.Meta):
        fields = PaymentSerializer.Meta.fields + ['customer_details', 'invoice_details']


class MpesaSTKPushSerializer(serializers.Serializer):
    """Serializer for M-Pesa STK Push requests"""
    customer_id = serializers.IntegerField(required=True)
    invoice_id = serializers.IntegerField(required=False)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=True)
    phone_number = serializers.CharField(max_length=20, required=True)
    
    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than zero")
        return value


class ReceiptSerializer(serializers.ModelSerializer):
    """Serializer for Receipt model"""
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True)
    company_name = serializers.CharField(source='company.name', read_only=True)
    payment_number = serializers.CharField(source='payment.payment_number', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    issued_by_name = serializers.CharField(source='issued_by.get_full_name', read_only=True)
    
    class Meta:
        model = Receipt
        fields = [
            'id', 'receipt_number', 'company', 'company_name', 'customer', 'customer_name',
            'customer_code', 'payment', 'payment_number', 'amount', 'amount_in_words',
            'currency', 'payment_method', 'payment_reference', 'status', 'receipt_date',
            'issued_at', 'issued_by', 'issued_by_name', 'notes', 'digital_signature',
            'qr_code', 'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'receipt_number', 'amount_in_words', 'digital_signature', 'qr_code',
            'issued_by', 'issued_at', 'created_by', 'created_at', 'updated_at'
        ]