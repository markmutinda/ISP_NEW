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
            'c2b_urls_registered', 'c2b_urls_registered_at',  # ADDED: Track URL registration status
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
            'c2b_urls_registered_at',  # ADDED: Registration timestamp is read-only
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
        required=False,
        allow_blank=True,
        help_text="Phone number to send test STK push to"
    )
    test_amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        min_value=1,
        help_text="Amount to test with (minimum 1 KES)"
    )

    def validate_test_phone(self, value):
        """Validate phone number format"""
        if not value:
            return value

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

    def validate(self, attrs):
        """If one STK test field is provided, require the other as well."""
        phone = attrs.get('test_phone')
        amount = attrs.get('test_amount')

        if (phone and amount is None) or (amount is not None and not phone):
            raise serializers.ValidationError(
                "Provide both test_phone and test_amount for an STK push test, or omit both for token-only testing."
            )

        return attrs


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
# Payment Method Serializers (UPDATED - With Frontend Bridge Validator)
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
            'mpesa_configuration', 'mpesa_configuration_details',
            'till_number', 'paybill_number', 'account_number', 'bank_name', 'custom_link', 'is_default',
            'is_active', 'requires_confirmation', 'confirmation_timeout',
            'transaction_fee', 'fee_type', 'minimum_amount', 'maximum_amount',
            'integration_class', 'config_json', 'status', 'last_used',
            'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at', 'last_used']

    # ═════════════════════════════════════════════════════════════════
    # VALIDATE METHOD - FRONTEND BRIDGING INTERCEPTOR
    # ═════════════════════════════════════════════════════════════════
    def validate(self, data):
        """
        Defensive interceptor to automatically unpack the frontend's nested 'config' 
        object and map properties to root model fields for Tuma API harmony.
        
        This bridges the gap between:
        - Frontend (page_5.tsx) sending: { config: { bank_name: "Cooperative Bank", ... } }
        - Backend expecting: config_json field or direct model fields
        
        When sync_active_method_to_tuma runs, it will automatically capture these
        valid inputs and create the proper settlement links on Tuma.
        """
        # Safely extract data from raw initial request data payload mapping layers
        initial_config = self.initial_data.get('config') or self.initial_data.get('config_json') or {}
        config_json = data.get('config_json') or {}
        
        # Merge configurations to catch parameters from either source context
        merged_config = {**config_json, **initial_config}
        mtype = data.get('method_type') or (self.instance.method_type if self.instance else None)

        if mtype == 'BANK_TRANSFER':
            bank_name = data.get('bank_name') or merged_config.get('bank_name')
            account_number = data.get('account_number') or merged_config.get('account_number')
            
            if not bank_name:
                raise serializers.ValidationError({"bank_name": "Bank name is required for bank transfers."})
            if not account_number:
                raise serializers.ValidationError({"account_number": "Account number is required for bank transfers."})
            
            # Enforce flat root storage and structured JSON alignment simultaneously
            data['bank_name'] = bank_name
            data['account_number'] = account_number
            data['config_json'] = {
                "bank_name": bank_name,
                "account_number": account_number
            }
            
        elif mtype == 'MPESA_TILL':
            till = data.get('till_number') or merged_config.get('till_number') or merged_config.get('shortcode')
            if till:
                data['till_number'] = till
                data['config_json'] = {"till_number": till}
                
        elif mtype == 'MPESA_PAYBILL':
            paybill = data.get('paybill_number') or merged_config.get('paybill_number') or merged_config.get('shortcode')
            if paybill:
                data['paybill_number'] = paybill
                data['config_json'] = {"paybill_number": paybill}

        elif mtype == 'MPESA_STK':
            # Handle STK Push settlement details (can be either paybill or till)
            paybill = data.get('paybill_number') or merged_config.get('paybill_number') or merged_config.get('shortcode')
            till = data.get('till_number') or merged_config.get('till_number')
            
            if paybill:
                data['paybill_number'] = paybill
                existing_config = data.get('config_json') or {}
                data['config_json'] = {**existing_config, "paybill_number": paybill}
            if till:
                data['till_number'] = till
                existing_config = data.get('config_json') or {}
                data['config_json'] = {**existing_config, "till_number": till}
                
        elif mtype == 'MOBILE_MONEY':
            phone = merged_config.get('phone_number') or merged_config.get('phone')
            provider = merged_config.get('mobile_provider') or 'SAFARICOM'
            
            if phone:
                existing_config = data.get('config_json') or {}
                data['config_json'] = {
                    **existing_config,
                    "phone_number": phone,
                    "mobile_provider": provider
                }

        return data


# ==========================
# Payment Serializers - FIXED VERSION WITH CUSTOMER_NAME, SERVICE_TYPE, PAYMENT_METHOD_NAME
# ==========================

class PaymentSerializer(serializers.ModelSerializer):
    """
    Serializer for Payment model - Returns strings instead of IDs for frontend compatibility.
    Enhanced to handle Hotspot payments where customer may be null.
    """
    # Custom fields for better frontend display
    customer_name = serializers.SerializerMethodField()
    service_type = serializers.SerializerMethodField()
    payment_method_name = serializers.SerializerMethodField()
    
    # Keep backward compatibility fields
    customer = serializers.CharField(source='customer.full_name', read_only=True, required=False)
    payment_method = serializers.CharField(source='payment_method.name', read_only=True, required=False)
    invoice = serializers.CharField(source='invoice.invoice_number', read_only=True, required=False)
    
    # Keep these for extra detail if needed
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True, required=False)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True, required=False)
    
    # Add M-Pesa transaction details as strings
    mpesa_receipt = serializers.CharField(read_only=True, required=False)
    transaction_id = serializers.CharField(read_only=True, required=False)
    
    # Include reference/payer fields for Hotspot
    reference = serializers.CharField(source='payment_reference', read_only=True, required=False)
    payer_name = serializers.CharField(read_only=True, required=False)
    payer_phone = serializers.CharField(read_only=True, required=False)

    class Meta:
        model = Payment
        fields = [
            'id', 'payment_number', 'customer', 'customer_name', 'customer_code',
            'invoice', 'amount', 'currency', 'payment_method', 'payment_method_name',
            'status', 'transaction_id', 'mpesa_receipt', 'service_type',
            'payment_date', 'created_by_name', 'created_at', 'reference',
            'payer_name', 'payer_phone'
        ]

    def get_customer_name(self, obj):
        """
        Get the customer name, handling null customers (e.g., Hotspot payments)
        """
        # 1. Try the linked PPPoE customer
        if obj.customer and hasattr(obj.customer, 'full_name') and obj.customer.full_name:
            return obj.customer.full_name
        
        # 2. Try the hotspot payer name (from STK push or direct)
        if obj.payer_name:
            return obj.payer_name
        
        # 3. Fallback to payer phone number if we have it
        if obj.payer_phone:
            return obj.payer_phone
        
        # 4. Check if there's a customer via related name
        if obj.customer and hasattr(obj.customer, 'name') and obj.customer.name:
            return obj.customer.name
        
        # 5. Final fallback
        return "Hotspot Client"

    def get_service_type(self, obj):
        """
        Determine if this payment was for Hotspot, PPPoE, or other service
        """
        # Check if linked to hotspot session
        if hasattr(obj, 'hotspot_session') and obj.hotspot_session:
            return "Hotspot"
        
        # Check if payment method name indicates hotspot
        if obj.payment_method and obj.payment_method.name and "Hotspot" in obj.payment_method.name:
            return "Hotspot"
        
        # Check if there's a customer (PPPoE usually has customer)
        if obj.customer:
            return "PPPoE"
        
        # Check if it's an M-Pesa payment without customer (likely hotspot)
        if obj.payment_method and obj.payment_method.method_type and obj.payment_method.method_type.startswith('MPESA_'):
            if not obj.customer:
                return "Hotspot"
        
        # Check payer_name presence (hotspot often has payer_name but no customer)
        if obj.payer_name and not obj.customer:
            return "Hotspot"
        
        return "Other"

    def get_payment_method_name(self, obj):
        """
        Get the payment method name safely
        """
        if obj.payment_method:
            return obj.payment_method.name
        return "Unknown"


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
    
    # Keep string versions for frontend display (override to return None safely)
    customer = serializers.SerializerMethodField()
    payment_method = serializers.SerializerMethodField()
    invoice = serializers.SerializerMethodField()

    class Meta(PaymentSerializer.Meta):
        fields = PaymentSerializer.Meta.fields + [
            'customer_details', 'invoice_details', 'mpesa_transaction_details'
        ]
    
    def get_customer(self, obj):
        return obj.customer.full_name if obj.customer else None
    
    def get_payment_method(self, obj):
        return obj.payment_method.name if obj.payment_method else None
    
    def get_invoice(self, obj):
        return obj.invoice.invoice_number if obj.invoice else None


class PaymentListSerializer(serializers.ModelSerializer):
    """
    Simplified serializer for listing payments with essential fields only.
    All fields return strings for frontend compatibility, including Hotspot support.
    """
    customer_name = serializers.SerializerMethodField()
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True, required=False)
    payment_method_name = serializers.CharField(source='payment_method.name', read_only=True, required=False)
    service_type = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            'id', 'payment_number', 'customer_name', 'customer_code', 
            'amount', 'payment_method_name', 'service_type',
            'status', 'transaction_id', 'mpesa_receipt', 'payment_date', 'created_at'
        ]

    def get_customer_name(self, obj):
        """Get customer name, handling null customers (Hotspot)"""
        if obj.customer and hasattr(obj.customer, 'full_name') and obj.customer.full_name:
            return obj.customer.full_name
        if obj.payer_name:
            return obj.payer_name
        if obj.payer_phone:
            return obj.payer_phone
        return "Hotspot Client"

    def get_service_type(self, obj):
        """Determine service type for the payment"""
        if hasattr(obj, 'hotspot_session') and obj.hotspot_session:
            return "Hotspot"
        if obj.payment_method and obj.payment_method.name and "Hotspot" in obj.payment_method.name:
            return "Hotspot"
        if obj.customer:
            return "PPPoE"
        if obj.payer_name and not obj.customer:
            return "Hotspot"
        return "Other"


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