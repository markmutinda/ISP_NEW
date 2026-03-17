# apps/billing/serializers/payment_serializers.py
from rest_framework import serializers
from django.utils import timezone
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db.models import Sum
from ..models.payment_models import MpesaConfiguration, MpesaTransaction, Payment, Receipt, InvoiceItemPayment

from customers.serializers import CustomerSerializer
from ..serializers.invoice_serializers import InvoiceSerializer


# ==========================
# M-Pesa Configuration Serializers
# ==========================

class MpesaConfigurationSerializer(serializers.ModelSerializer):
    """
    Serializer for M-Pesa Configuration - used for tenant-specific M-Pesa settings
    """
    class Meta:
        model = MpesaConfiguration
        fields = [
            'id', 'business_shortcode', 'shortcode_type', 'passkey', 
            'consumer_key', 'consumer_secret', 'callback_url', 'timeout_url',
            'is_sandbox', 'is_active', 'is_default', 'test_mode',
            'last_validated_at', 'validation_status', 'validation_error',
            'daily_transaction_limit', 'min_transaction_amount', 'max_transaction_amount',
            'created_at', 'updated_at'
        ]
        # Make secrets write-only so they don't get exposed in API responses
        extra_kwargs = {
            'passkey': {'write_only': True, 'required': False},
            'consumer_secret': {'write_only': True, 'required': True},
            'consumer_key': {'required': True},
            'business_shortcode': {'required': True},
        }
        read_only_fields = [
            'id', 'last_validated_at', 'validation_status', 'validation_error',
            'created_at', 'updated_at'
        ]

    def validate_business_shortcode(self, value):
        """Validate that shortcode is numeric and proper length"""
        if not value.isdigit():
            raise serializers.ValidationError("Business shortcode must contain only numbers")
        if len(value) < 5 or len(value) > 7:
            raise serializers.ValidationError("Business shortcode must be 5-7 digits long")
        return value

    def validate(self, data):
        """Cross-field validation"""
        # If this is set as default, ensure it's also active
        if data.get('is_default') and not data.get('is_active', False):
            raise serializers.ValidationError(
                "A default configuration must also be active. "
                "Please set is_active=True or is_default=False."
            )
        
        # Validate min/max amounts
        if data.get('min_transaction_amount') and data.get('max_transaction_amount'):
            if data['min_transaction_amount'] > data['max_transaction_amount']:
                raise serializers.ValidationError(
                    "Minimum transaction amount cannot exceed maximum transaction amount"
                )
        
        return data


class MpesaConfigurationDetailSerializer(MpesaConfigurationSerializer):
    """
    Detailed serializer for M-Pesa Configuration including usage statistics
    """
    transaction_count = serializers.SerializerMethodField()
    total_transaction_amount = serializers.SerializerMethodField()
    last_transaction_date = serializers.SerializerMethodField()
    
    class Meta(MpesaConfigurationSerializer.Meta):
        fields = MpesaConfigurationSerializer.Meta.fields + [
            'transaction_count', 'total_transaction_amount', 'last_transaction_date'
        ]
    
    def get_transaction_count(self, obj):
        """Get total number of transactions using this configuration"""
        if hasattr(obj, 'transactions'):
            return obj.transactions.count()
        return 0
    
    def get_total_transaction_amount(self, obj):
        """Get total amount processed through this configuration"""
        if hasattr(obj, 'transactions'):
            total = obj.transactions.filter(
                status='COMPLETED'
            ).aggregate(total=Sum('amount'))['total']
            return total or Decimal('0.00')
        return Decimal('0.00')
    
    def get_last_transaction_date(self, obj):
        """Get the date of the last transaction"""
        if hasattr(obj, 'transactions'):
            last = obj.transactions.order_by('-created_at').first()
            return last.created_at if last else None
        return None


class MpesaConfigurationTestSerializer(serializers.Serializer):
    """
    Serializer for testing M-Pesa configuration
    """
    test_phone = serializers.CharField(
        max_length=20,
        required=True,
        help_text="Phone number to send test STK push to"
    )
    test_amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=True,
        min_value=1,
        help_text="Amount to test with (minimum 1 KES)"
    )

    def validate_test_phone(self, value):
        """Validate phone number format"""
        # Remove any non-digit characters
        phone = ''.join(filter(str.isdigit, value))
        
        # Check if it's a valid Kenyan phone number
        if len(phone) == 9 and phone.startswith('7'):
            phone = '254' + phone
        elif len(phone) == 10 and phone.startswith('07'):
            phone = '254' + phone[1:]
        elif len(phone) == 12 and phone.startswith('254'):
            pass  # Already in correct format
        else:
            raise serializers.ValidationError(
                "Please enter a valid Kenyan phone number (e.g., 0712345678 or 254712345678)"
            )
        
        return phone


# ==========================
# M-Pesa Transaction Serializers
# ==========================

class MpesaTransactionSerializer(serializers.ModelSerializer):
    """
    Serializer for M-Pesa Transaction records
    """
    customer_name = serializers.CharField(source='payment.customer.full_name', read_only=True)
    customer_phone = serializers.CharField(source='payment.payer_phone', read_only=True)
    payment_number = serializers.CharField(source='payment.payment_number', read_only=True)
    configuration_shortcode = serializers.CharField(
        source='configuration.business_shortcode', 
        read_only=True
    )
    
    class Meta:
        model = MpesaTransaction
        fields = [
            'id', 'payment', 'payment_number', 'customer_name', 'customer_phone',
            'configuration', 'configuration_shortcode', 'merchant_request_id',
            'checkout_request_id', 'transaction_id', 'transaction_type', 'amount',
            'phone_number', 'account_reference', 'transaction_desc', 'status',
            'result_code', 'result_desc', 'callback_data', 'callback_received_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'merchant_request_id', 'checkout_request_id', 'transaction_id',
            'result_code', 'result_desc', 'callback_data', 'callback_received_at',
            'created_at', 'updated_at'
        ]


class MpesaTransactionDetailSerializer(MpesaTransactionSerializer):
    """
    Detailed serializer for M-Pesa Transaction including request/response payloads
    """
    request_payload = serializers.JSONField(read_only=True)
    response_payload = serializers.JSONField(read_only=True)
    
    class Meta(MpesaTransactionSerializer.Meta):
        fields = MpesaTransactionSerializer.Meta.fields + [
            'request_payload', 'response_payload'
        ]


class MpesaCallbackSerializer(serializers.Serializer):
    """
    Serializer for validating M-Pesa callbacks
    """
    Body = serializers.DictField(required=True)
    
    def validate_Body(self, value):
        """Validate the callback body structure"""
        if 'stkCallback' not in value:
            raise serializers.ValidationError("Invalid callback format: missing stkCallback")
        
        callback_data = value['stkCallback']
        required_fields = ['MerchantRequestID', 'CheckoutRequestID', 'ResultCode', 'ResultDesc']
        
        for field in required_fields:
            if field not in callback_data:
                raise serializers.ValidationError(f"Invalid callback format: missing {field}")
        
        return value


# ==========================
# Payment Method Serializers
# ==========================

class PaymentMethodSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    
    # Add M-Pesa configuration field
    mpesa_configuration_details = MpesaConfigurationSerializer(
        source='mpesa_configuration', 
        read_only=True
    )

    class Meta:
        model = InvoiceItemPayment
        fields = [
            'id', 'name', 'code', 'method_type', 'description',
            'channel_id', 'is_payhero_enabled', 'mpesa_configuration', 'mpesa_configuration_details',
            'till_number', 'paybill_number', 'account_number', 'bank_name', 'custom_link', 'is_default',
            'is_active', 'requires_confirmation', 'confirmation_timeout',
            'transaction_fee', 'fee_type', 'minimum_amount', 'maximum_amount',
            'integration_class', 'config_json', 'status', 'last_used',
            'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at', 'last_used']

    def validate(self, data):
        if data.get('is_payhero_enabled') and not data.get('channel_id'):
            raise serializers.ValidationError({"channel_id": "This field is required when PayHero is enabled."})
        
        # Validate M-Pesa configuration for M-Pesa payment methods
        method_type = data.get('method_type')
        mpesa_config = data.get('mpesa_configuration')
        
        if method_type and method_type.startswith('MPESA_') and not mpesa_config:
            raise serializers.ValidationError({
                "mpesa_configuration": f"M-Pesa configuration is required for {method_type} payment methods."
            })
        
        return data


# ==========================
# Payment Serializers - FIXED VERSION
# ==========================

class PaymentSerializer(serializers.ModelSerializer):
    """
    Serializer for Payment model - Returns strings instead of IDs for frontend compatibility
    """
    # OVERRIDE: Return strings instead of IDs to satisfy frontend formatting
    customer = serializers.CharField(source='customer.full_name', read_only=True)
    payment_method = serializers.CharField(source='payment_method.name', read_only=True)
    invoice = serializers.CharField(source='invoice.invoice_number', read_only=True)
    
    # Keep these for extra detail if needed
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    
    # Add M-Pesa transaction details as strings
    mpesa_receipt = serializers.CharField(read_only=True)
    transaction_id = serializers.CharField(read_only=True)

    class Meta:
        model = Payment
        fields = [
            'id', 'payment_number', 'customer', 'customer_code',
            'invoice', 'amount', 'currency', 'payment_method', 
            'status', 'transaction_id', 'mpesa_receipt', 
            'payment_date', 'created_by_name', 'created_at'
        ]


class PaymentCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = [
            'customer', 'invoice', 'amount', 'payment_method', 'payment_reference',
            'transaction_id', 'payer_name', 'payer_phone', 'payer_email',
            'payer_id_number', 'bank_name', 'account_number', 'branch',
            'cheque_number', 'mpesa_receipt', 'mpesa_phone', 'mpesa_name', 'notes'
        ]

    def validate(self, data):
        amount = data.get('amount')
        if amount and amount <= 0:
            raise serializers.ValidationError("Amount must be greater than zero")
        
        payment_method = data.get('payment_method')
        if payment_method:
            # Need to import model here to access properties
            from ..models.payment_models import InvoiceItemPayment
            if isinstance(payment_method, int):
                try:
                    payment_method = InvoiceItemPayment.objects.get(id=payment_method)
                except InvoiceItemPayment.DoesNotExist:
                    raise serializers.ValidationError("Payment method does not exist")
            
            if not payment_method.is_active:
                raise serializers.ValidationError("Payment method is not active")
            if amount and not payment_method.is_amount_valid(amount):
                raise serializers.ValidationError(
                    f"Amount must be between {payment_method.minimum_amount} "
                    f"and {payment_method.maximum_amount}"
                )
            
            # Validate M-Pesa configuration for M-Pesa payments
            if payment_method.method_type and payment_method.method_type.startswith('MPESA_'):
                if not payment_method.mpesa_configuration:
                    raise serializers.ValidationError(
                        "This M-Pesa payment method is not properly configured. "
                        "Please contact support."
                    )
                if not payment_method.mpesa_configuration.is_active:
                    raise serializers.ValidationError(
                        "M-Pesa service is currently disabled. Please try another payment method."
                    )
        
        return data


class PaymentDetailSerializer(PaymentSerializer):
    """
    Detailed serializer for single payment view - includes full related objects
    """
    customer_details = CustomerSerializer(source='customer', read_only=True)
    invoice_details = InvoiceSerializer(source='invoice', read_only=True)
    
    # Add full M-Pesa transaction details
    mpesa_transaction_details = MpesaTransactionSerializer(
        source='mpesa_transaction', 
        read_only=True
    )
    
    # Keep string versions for frontend display
    customer = serializers.CharField(source='customer.full_name', read_only=True)
    payment_method = serializers.CharField(source='payment_method.name', read_only=True)
    invoice = serializers.CharField(source='invoice.invoice_number', read_only=True)

    class Meta(PaymentSerializer.Meta):
        fields = PaymentSerializer.Meta.fields + [
            'customer_details', 'invoice_details', 'mpesa_transaction_details'
        ]


class PaymentListSerializer(serializers.ModelSerializer):
    """
    Simplified serializer for listing payments with essential fields only
    All fields return strings for frontend compatibility
    """
    customer = serializers.CharField(source='customer.full_name', read_only=True)
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True)
    payment_method = serializers.CharField(source='payment_method.name', read_only=True)

    class Meta:
        model = Payment
        fields = [
            'id', 'payment_number', 'customer', 'customer_code', 
            'amount', 'payment_method', 'status', 
            'transaction_id', 'mpesa_receipt', 'payment_date', 'created_at'
        ]


# ==========================
# M-Pesa STK Push Serializer
# ==========================

class MpesaSTKPushSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField(required=True)
    invoice_id = serializers.IntegerField(required=False, allow_null=True)
    service_connection_id = serializers.IntegerField(
        required=False, 
        allow_null=True,
        help_text="ID of the service connection to pay for (if not paying an invoice)"
    )
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=True)
    phone_number = serializers.CharField(max_length=20, required=True)
    account_reference = serializers.CharField(
        max_length=50, 
        required=False,
        help_text="Account reference to use (defaults to customer's billing account number)"
    )
    transaction_desc = serializers.CharField(
        max_length=200, 
        required=False,
        default="Payment for Internet Services"
    )

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than zero")
        if value > Decimal('150000.00'):
            raise serializers.ValidationError("Amount cannot exceed M-Pesa limit of 150,000 KES")
        return value

    def validate_phone_number(self, value):
        """Validate and format phone number"""
        # Remove any non-digit characters
        phone = ''.join(filter(str.isdigit, value))
        
        # Convert to international format
        if len(phone) == 9 and phone.startswith('7'):
            phone = '254' + phone
        elif len(phone) == 10 and phone.startswith('07'):
            phone = '254' + phone[1:]
        elif len(phone) == 12 and phone.startswith('254'):
            pass  # Already in correct format
        else:
            raise serializers.ValidationError(
                "Please enter a valid Kenyan phone number (e.g., 0712345678 or 254712345678)"
            )
        
        return phone

    def validate(self, data):
        """Ensure either invoice_id or service_connection_id is provided"""
        if not data.get('invoice_id') and not data.get('service_connection_id'):
            raise serializers.ValidationError(
                "Either invoice_id or service_connection_id must be provided"
            )
        return data


# ==========================
# Receipt Serializers
# ==========================

class ReceiptSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    payment_number = serializers.CharField(source='payment.payment_number', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)

    class Meta:
        model = Receipt
        fields = [
            'id', 'receipt_number', 'customer', 'customer_name', 'payment', 
            'payment_number', 'amount', 'amount_in_words', 'currency', 
            'payment_method', 'payment_reference', 'status', 'receipt_date',
            'issued_at', 'issued_by', 'notes', 'digital_signature',
            'qr_code', 'created_by_name', 'created_at'
        ]
        read_only_fields = [
            'receipt_number', 'amount_in_words', 'digital_signature', 'qr_code',
            'issued_at', 'created_at'
        ]


class ReceiptCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Receipt
        fields = ['payment', 'notes']

    def validate(self, data):
        payment = data.get('payment')
        
        # Check if receipt already exists for this payment
        from ..models.payment_models import Receipt
        if Receipt.objects.filter(payment=payment).exists():
            raise serializers.ValidationError(
                f"A receipt already exists for payment {payment.payment_number}"
            )
        
        # Ensure payment is completed
        if payment.status != 'COMPLETED':
            raise serializers.ValidationError(
                "Receipts can only be created for completed payments"
            )
        
        return data


class ReceiptListSerializer(serializers.ModelSerializer):
    """
    Simplified serializer for listing receipts with essential fields only
    """
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    payment_number = serializers.CharField(source='payment.payment_number', read_only=True)

    class Meta:
        model = Receipt
        fields = [
            'id', 'receipt_number', 'customer', 'customer_name', 'payment', 
            'payment_number', 'amount', 'status', 'receipt_date'
        ]