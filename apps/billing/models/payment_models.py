# apps/billing/models/payment_models.py
from django.db import models
from django.core.validators import MinValueValidator
from django.core.exceptions import ValidationError
from django.utils import timezone
from decimal import Decimal
from apps.core.models import Company, AuditMixin
#from apps.customers.models import Customer
from .billing_models import Invoice
from django.conf import settings


class MpesaConfiguration(AuditMixin):
    """
    Tenant-specific M-Pesa Paybill credentials.
    Each tenant (ISP) configures their own Paybill here.
    """
    # Tenant schema field to isolate configurations
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        default="default_schema"
    )

    # Core Paybill Details
    business_shortcode = models.CharField(
        max_length=20, 
        help_text="The Paybill/Till Number (e.g., 123456)"
    )
    shortcode_type = models.CharField(
        max_length=10,
        choices=[('PAYBILL', 'Paybill'), ('TILL', 'Till Number')],
        default='PAYBILL',
        help_text="Type of shortcode (Paybill or Till)"
    )
    passkey = models.CharField(
        max_length=255, 
        blank=True, 
        null=True, 
        help_text="Lipa Na M-Pesa Online Passkey (for STK Push)"
    )
    
    # API Credentials from Daraja Portal
    consumer_key = models.CharField(max_length=255, help_text="Daraja App Consumer Key")
    consumer_secret = models.CharField(max_length=255, help_text="Daraja App Consumer Secret")
    
    # Callback URLs - can be overridden per tenant
    callback_url = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Override default callback URL (optional)"
    )
    timeout_url = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Override default timeout URL (optional)"
    )
    
    # Environment
    is_sandbox = models.BooleanField(default=True, help_text="Use Daraja Sandbox environment")
    
    # Status
    is_active = models.BooleanField(
        default=False, 
        help_text="Enable/Disable M-Pesa for this tenant"
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Default configuration for this tenant (used when multiple exist)"
    )
    
    # Test Mode
    test_mode = models.BooleanField(
        default=False,
        help_text="In test mode, transactions are simulated without real money"
    )
    
    # Validation timestamps
    last_validated_at = models.DateTimeField(
        null=True, 
        blank=True,
        help_text="When the credentials were last successfully validated"
    )
    validation_status = models.CharField(
        max_length=20,
        choices=[('PENDING', 'Pending'), ('VALID', 'Valid'), ('INVALID', 'Invalid')],
        default='PENDING',
        help_text="Status of last credential validation"
    )
    validation_error = models.TextField(blank=True, help_text="Error message from last validation")
    
    # Usage limits
    daily_transaction_limit = models.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        null=True, 
        blank=True,
        help_text="Maximum total transaction amount per day"
    )
    min_transaction_amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=1.00,
        help_text="Minimum allowed transaction amount"
    )
    max_transaction_amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=150000.00,
        help_text="Maximum allowed transaction amount (Safaricom limit is 150K)"
    )
    
    class Meta:
        verbose_name = 'M-Pesa Configuration'
        verbose_name_plural = 'M-Pesa Configurations'
        indexes = [
            models.Index(fields=['schema_name', 'is_active']),
            models.Index(fields=['schema_name', 'is_default']),
            models.Index(fields=['business_shortcode', 'shortcode_type']),
        ]
        unique_together = [
            ['schema_name', 'business_shortcode', 'shortcode_type'],  # Unique shortcode per tenant
        ]
        
    def __str__(self):
        env = "Sandbox" if self.is_sandbox else "Production"
        status = "✓" if self.is_active else "✗"
        default = " (Default)" if self.is_default else ""
        return f"{self.shortcode_type}: {self.business_shortcode} [{env}]{default} {status}"
    
    def clean(self):
        """Validate configuration before saving"""
        # Validate transaction amount limits
        if self.min_transaction_amount < Decimal('0.01'):
            raise ValidationError({'min_transaction_amount': 'Minimum amount must be at least 0.01'})
            
        if self.max_transaction_amount > Decimal('150000.00'):
            raise ValidationError({'max_transaction_amount': 'Amount cannot exceed Safaricom limit of 150,000'})
            
        if self.min_transaction_amount > self.max_transaction_amount:
            raise ValidationError('Minimum amount cannot exceed maximum amount')
    
    def save(self, *args, **kwargs):
        # Ensure only one active configuration per tenant
        if self.is_active:
            MpesaConfiguration.objects.filter(
                schema_name=self.schema_name, 
                is_active=True
            ).exclude(pk=self.pk).update(is_active=False)
        
        # Ensure only one default configuration per tenant
        if self.is_default:
            MpesaConfiguration.objects.filter(
                schema_name=self.schema_name,
                is_default=True
            ).exclude(pk=self.pk).update(is_default=False)
        
        # If this is the first configuration for this tenant, make it default and active
        if not self.pk and not MpesaConfiguration.objects.filter(schema_name=self.schema_name).exists():
            self.is_default = True
            self.is_active = True
        
        self.full_clean()  # Run validations
        super().save(*args, **kwargs)
    
    @classmethod
    def get_active_configuration(cls, schema_name):
        """
        Get the active M-Pesa configuration for a tenant.
        Returns the active config or None.
        """
        return cls.objects.filter(
            schema_name=schema_name,
            is_active=True
        ).first()
    
    @classmethod
    def get_default_configuration(cls, schema_name):
        """
        Get the default M-Pesa configuration for a tenant.
        Falls back to active if no default is set.
        """
        config = cls.objects.filter(
            schema_name=schema_name,
            is_default=True
        ).first()
        
        if not config:
            config = cls.get_active_configuration(schema_name)
        
        return config
    
    def get_api_environment(self):
        """Return the appropriate API environment settings"""
        if self.is_sandbox:
            return "sandbox"
        else:
            return "production"
    
    def get_callback_url(self, request=None):
        """
        Generate the callback URL for this configuration.
        Uses tenant-specific override if provided, otherwise generates dynamically.
        """
        if self.callback_url:
            return self.callback_url
        
        # Generate dynamic callback URL
        base_url = getattr(settings, 'BASE_URL', 'https://example.com')
        return f"{base_url}/api/billing/mpesa/callback/{self.schema_name}/"
    
    def get_timeout_url(self, request=None):
        """Generate the timeout URL for this configuration"""
        if self.timeout_url:
            return self.timeout_url
        
        base_url = getattr(settings, 'BASE_URL', 'https://example.com')
        return f"{base_url}/api/billing/mpesa/timeout/{self.schema_name}/"


class MpesaTransaction(models.Model):
    """
    Track M-Pesa transactions for reconciliation and audit
    """
    TRANSACTION_STATUS = [
        ('PENDING', 'Pending'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('TIMEOUT', 'Timeout'),
        ('CANCELLED', 'Cancelled'),
    ]
    
    TRANSACTION_TYPE = [
        ('STK_PUSH', 'STK Push'),
        ('C2B', 'Customer to Business'),
        ('B2C', 'Business to Customer'),
        ('B2B', 'Business to Business'),
        ('QUERY', 'Transaction Query'),
        ('REVERSAL', 'Transaction Reversal'),
    ]
    
    # Link to payment if created
    # FIXED: Changed related_name from 'mpesa_transaction' to 'payment_log' to avoid conflict
    payment = models.OneToOneField(
        'billing.Payment', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='payment_log'
    )
    
    # Link to configuration
    configuration = models.ForeignKey(
        'billing.MpesaConfiguration', 
        on_delete=models.PROTECT,
        related_name='transactions'
    )
    
    # Tenant schema field
    schema_name = models.SlugField(max_length=63, editable=False)
    
    # Transaction identifiers
    merchant_request_id = models.CharField(max_length=100, unique=True)
    checkout_request_id = models.CharField(max_length=100, unique=True)
    transaction_id = models.CharField(max_length=50, blank=True, db_index=True)  # M-Pesa receipt
    
    # Transaction details
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPE, default='STK_PUSH')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    phone_number = models.CharField(max_length=20)
    account_reference = models.CharField(max_length=50, blank=True)
    transaction_desc = models.CharField(max_length=200, blank=True)
    
    # Status
    status = models.CharField(max_length=20, choices=TRANSACTION_STATUS, default='PENDING')
    result_code = models.IntegerField(null=True, blank=True)
    result_desc = models.TextField(blank=True)
    
    # Callback data
    callback_data = models.JSONField(null=True, blank=True)
    callback_received_at = models.DateTimeField(null=True, blank=True)
    
    # Request/Response logging
    request_payload = models.JSONField(null=True, blank=True)
    response_payload = models.JSONField(null=True, blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['merchant_request_id']),
            models.Index(fields=['checkout_request_id']),
            models.Index(fields=['transaction_id']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['schema_name', 'status']),
        ]
    
    def __str__(self):
        return f"{self.transaction_id or 'Pending'} - {self.amount} - {self.status}"
    
    def save(self, *args, **kwargs):
        # Auto-set schema_name from configuration if not set
        if not self.schema_name and self.configuration:
            self.schema_name = self.configuration.schema_name
        super().save(*args, **kwargs)
    
    def mark_completed(self, transaction_id, callback_data=None):
        """Mark transaction as completed"""
        self.status = 'COMPLETED'
        self.transaction_id = transaction_id
        self.result_code = 0
        self.result_desc = "Success"
        if callback_data:
            self.callback_data = callback_data
            self.callback_received_at = timezone.now()
        self.save()
    
    def mark_failed(self, result_code, result_desc, callback_data=None):
        """Mark transaction as failed"""
        self.status = 'FAILED'
        self.result_code = result_code
        self.result_desc = result_desc
        if callback_data:
            self.callback_data = callback_data
            self.callback_received_at = timezone.now()
        self.save()
    
    def mark_timeout(self):
        """Mark transaction as timed out"""
        self.status = 'TIMEOUT'
        self.result_desc = "Transaction timed out - no callback received"
        self.save()


class InvoiceItemPayment(models.Model):
    METHOD_TYPES = [
        ('MPESA_STK', 'M-Pesa STK Push'),
        ('MPESA_TILL', 'M-Pesa Till'),
        ('MPESA_PAYBILL', 'M-Pesa Paybill'),
        ('BANK_TRANSFER', 'Bank Transfer'),
        ('PAYMENT_LINK', 'Payment Link'),
        ('CASH', 'Cash'),
        ('CHEQUE', 'Cheque'),
        ('CREDIT_CARD', 'Credit Card'),
        ('DEBIT_CARD', 'Debit Card'),
        ('MOBILE_MONEY', 'Mobile Money'),
        ('VOUCHER', 'Voucher'),
        ('OTHER', 'Other'),
    ]

    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
        ('MAINTENANCE', 'Under Maintenance'),
    ]

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20, unique=True)
    method_type = models.CharField(max_length=20, choices=METHOD_TYPES)
    description = models.TextField(blank=True)

    # PayHero Integration Fields
    channel_id = models.IntegerField(null=True, blank=True, help_text="PayHero channel ID")
    is_payhero_enabled = models.BooleanField(default=False, help_text="Route payments via PayHero")
    till_number = models.CharField(max_length=20, null=True, blank=True)
    paybill_number = models.CharField(max_length=20, null=True, blank=True)
    account_number = models.CharField(max_length=50, null=True, blank=True)
    bank_name = models.CharField(max_length=100, null=True, blank=True)
    custom_link = models.URLField(null=True, blank=True)
    is_default = models.BooleanField(default=False, help_text="Default payment method for this company")

    # M-Pesa Configuration Link
    mpesa_configuration = models.ForeignKey(
        'billing.MpesaConfiguration',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payment_methods',
        help_text="Link to tenant-specific M-Pesa configuration"
    )

    # Configuration
    is_active = models.BooleanField(default=True)
    requires_confirmation = models.BooleanField(default=False)
    confirmation_timeout = models.PositiveIntegerField(help_text="Timeout in minutes", default=30)

    # Fees
    transaction_fee = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    fee_type = models.CharField(max_length=10, choices=[('PERCENTAGE', 'Percentage'), ('FIXED', 'Fixed')], default='FIXED')

    # Limits
    minimum_amount = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    maximum_amount = models.DecimalField(max_digits=10, decimal_places=2, default=1000000)

    # Integration
    integration_class = models.CharField(max_length=100, blank=True)
    config_json = models.JSONField(default=dict, blank=True)

    # Tenant schema field
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        default="default_schema"
    )
    
    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    last_used = models.DateTimeField(null=True, blank=True)

    # Metadata
    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='created_payment_methods')
    updated_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='updated_payment_methods')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['method_type']),
            models.Index(fields=['is_active']),
            models.Index(fields=['channel_id']),
            models.Index(fields=['is_default']),
            models.Index(fields=['schema_name', 'method_type']),
        ]

    def __str__(self):
        payhero_status = " (PayHero)" if self.is_payhero_enabled else ""
        mpesa_status = " (M-Pesa)" if self.mpesa_configuration else ""
        return f"{self.name}{payhero_status}{mpesa_status}"

    def calculate_fee(self, amount):
        if self.fee_type == 'PERCENTAGE':
            return (amount * self.transaction_fee) / 100
        return self.transaction_fee

    def is_amount_valid(self, amount):
        return self.minimum_amount <= amount <= self.maximum_amount
    
    def get_mpesa_config(self):
        """Get the M-Pesa configuration for this payment method"""
        if self.mpesa_configuration and self.mpesa_configuration.is_active:
            return self.mpesa_configuration
        # Fall back to default tenant configuration
        return MpesaConfiguration.get_default_configuration(self.schema_name)


class Payment(models.Model):
    PAYMENT_STATUS = [
        ('PENDING', 'Pending'),
        ('PROCESSING', 'Processing'),
        ('COMPLETED', 'Completed'),
        ('FAILED', 'Failed'),
        ('CANCELLED', 'Cancelled'),
        ('REFUNDED', 'Refunded'),
        ('DISPUTED', 'Disputed'),
    ]

    payment_number = models.CharField(max_length=50, unique=True)
    customer = models.ForeignKey('customers.Customer', on_delete=models.CASCADE, related_name='payments')
    invoice = models.ForeignKey(Invoice, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')

    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0.01)])
    transaction_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default='KES')

    payment_method = models.ForeignKey('billing.InvoiceItemPayment', on_delete=models.PROTECT, related_name='payments')
    payment_reference = models.CharField(max_length=100, blank=True)
    transaction_id = models.CharField(max_length=100, blank=True)

    # PayHero-specific fields
    payhero_external_reference = models.CharField(max_length=255, blank=True, null=True, unique=True)
    raw_callback = models.JSONField(null=True, blank=True)
    
    # M-Pesa Transaction Link
    # FIXED: Changed related_name from 'related_payment' to 'payment_record' for clarity
    mpesa_transaction = models.OneToOneField(
        'billing.MpesaTransaction',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payment_record'
    )

    # Tenant schema field
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        default="default_schema"
    )
    
    status = models.CharField(max_length=20, choices=PAYMENT_STATUS, default='PENDING')
    is_reconciled = models.BooleanField(default=False)

    payment_date = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    reconciled_at = models.DateTimeField(null=True, blank=True)

    payer_name = models.CharField(max_length=200, blank=True)
    payer_phone = models.CharField(max_length=20, blank=True)
    payer_email = models.EmailField(blank=True)
    payer_id_number = models.CharField(max_length=50, blank=True)

    bank_name = models.CharField(max_length=100, blank=True)
    account_number = models.CharField(max_length=50, blank=True)
    branch = models.CharField(max_length=100, blank=True)
    cheque_number = models.CharField(max_length=50, blank=True)

    mpesa_receipt = models.CharField(max_length=50, blank=True)
    mpesa_phone = models.CharField(max_length=20, blank=True)
    mpesa_name = models.CharField(max_length=200, blank=True)

    notes = models.TextField(blank=True)
    failure_reason = models.TextField(blank=True)

    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='created_payments')
    processed_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='processed_payments')
    reconciled_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='reconciled_payments')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-payment_date']
        indexes = [
            models.Index(fields=['payment_number']),
            models.Index(fields=['customer', 'status']),
            models.Index(fields=['payment_date']),
            models.Index(fields=['transaction_id']),
            models.Index(fields=['mpesa_receipt']),
            models.Index(fields=['payhero_external_reference']),
            models.Index(fields=['schema_name', 'status', 'payment_date']),
        ]

    def __str__(self):
        return f"Payment #{self.payment_number} - {self.customer.customer_code}"

    def save(self, *args, **kwargs):
        if not self.payment_number:
            date_str = timezone.now().strftime('%Y%m%d')
            last_payment = Payment.objects.filter(payment_number__startswith=f'PAY-{date_str}').order_by('-payment_number').first()
            if last_payment and last_payment.payment_number:
                try:
                    last_num = int(last_payment.payment_number.split('-')[-1])
                    new_num = last_num + 1
                except (IndexError, ValueError):
                    new_num = Payment.objects.count() + 1
            else:
                new_num = 1
            self.payment_number = f"PAY-{date_str}-{new_num:05d}"

        if not self.net_amount:
            self.net_amount = self.amount - self.transaction_fee

        if not self.payer_name and self.customer:
            self.payer_name = self.customer.full_name
        if not self.payer_phone and self.customer:
            self.payer_phone = getattr(self.customer.user, 'phone_number', '')
        if not self.payer_email and self.customer:
            self.payer_email = getattr(self.customer.user, 'email', '')

        super().save(*args, **kwargs)

        if self.invoice and self.status == 'COMPLETED':
            self.invoice.add_payment(self.amount, self.payment_method)

    def mark_as_completed(self, processed_by=None):
        if self.status in ['PENDING', 'PROCESSING']:
            self.status = 'COMPLETED'
            self.processed_at = timezone.now()
            if processed_by:
                self.processed_by = processed_by
            self.save()
            return True
        return False

    def mark_as_failed(self, reason=""):
        if self.status in ['PENDING', 'PROCESSING']:
            self.status = 'FAILED'
            self.failure_reason = reason
            self.save()
            return True
        return False

    def refund(self, refund_amount=None, refund_reason=""):
        if self.status != 'COMPLETED':
            return None
        refund_amount = refund_amount or self.amount

        if refund_amount > self.amount:
            return None

        refund_payment = Payment.objects.create(
            customer=self.customer,
            amount=-refund_amount,
            payment_method=self.payment_method,
            status='COMPLETED',
            payment_reference=f"REFUND-{self.payment_number}",
            notes=f"Refund for {self.payment_number}. Reason: {refund_reason}",
            created_by=self.created_by,
            schema_name=self.schema_name
        )

        self.status = 'REFUNDED'
        self.save()

        if self.invoice:
            self.invoice.amount_paid -= refund_amount
            self.invoice.balance += refund_amount
            self.invoice.status = 'PARTIAL' if self.invoice.amount_paid > 0 else 'ISSUED'
            self.invoice.save()

        return refund_payment


class Receipt(models.Model):
    RECEIPT_STATUS = [
        ('DRAFT', 'Draft'),
        ('ISSUED', 'Issued'),
        ('CANCELLED', 'Cancelled'),
    ]

    # Basic Information
    receipt_number = models.CharField(max_length=50, unique=True)
    customer = models.ForeignKey('customers.Customer', on_delete=models.CASCADE, related_name='receipts')
    
    # Payment Reference
    payment = models.OneToOneField(Payment, on_delete=models.CASCADE, related_name='receipt')
    
    # Amount
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    amount_in_words = models.CharField(max_length=500)
    currency = models.CharField(max_length=3, default='KES')
    
    # Payment Details
    payment_method = models.CharField(max_length=100)
    payment_reference = models.CharField(max_length=100, blank=True)
    
    # Tenant schema field
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        default="default_schema"
    )
    
    # Status
    status = models.CharField(max_length=20, choices=RECEIPT_STATUS, default='DRAFT')
    
    # Dates
    receipt_date = models.DateTimeField(default=timezone.now)
    issued_at = models.DateTimeField(null=True, blank=True)
    
    # Issuer
    issued_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='issued_receipts')
    
    # Notes
    notes = models.TextField(blank=True)
    
    # Digital Signature
    digital_signature = models.TextField(blank=True)
    qr_code = models.TextField(blank=True)
    
    # Metadata
    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='created_receipts')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-receipt_date']
        indexes = [
            models.Index(fields=['receipt_number']),
            models.Index(fields=['customer', 'receipt_date']),
            models.Index(fields=['payment']),
            models.Index(fields=['schema_name', 'receipt_date']),
        ]

    def __str__(self):
        return f"Receipt #{self.receipt_number}"

    def save(self, *args, **kwargs):
        if not self.receipt_number:
            # Generate receipt number: RCPT-YYYY-XXXXX
            year = timezone.now().year
            last_receipt = Receipt.objects.filter(
                receipt_number__startswith=f'RCPT-{year}'
            ).order_by('-receipt_number').first()
            
            if last_receipt and last_receipt.receipt_number:
                try:
                    last_num = int(last_receipt.receipt_number.split('-')[-1])
                    new_num = last_num + 1
                except (IndexError, ValueError):
                    new_num = Receipt.objects.count() + 1
            else:
                new_num = 1
            
            self.receipt_number = f"RCPT-{year}-{new_num:05d}"
        
        # Set amount from payment if not set
        if not self.amount and self.payment:
            self.amount = self.payment.amount
        
        # Set payment method from payment if not set
        if not self.payment_method and self.payment:
            self.payment_method = self.payment.payment_method.name
        
        # Set payment reference from payment if not set
        if not self.payment_reference and self.payment:
            self.payment_reference = self.payment.payment_reference
        
        # Set schema_name from payment if not set
        if not self.schema_name and self.payment:
            self.schema_name = self.payment.schema_name
        
        super().save(*args, **kwargs)

    def issue_receipt(self, user):
        if self.status == 'DRAFT':
            self.status = 'ISSUED'
            self.issued_by = user
            self.issued_at = timezone.now()
            
            # Generate amount in words
            try:
                from utils.helpers import number_to_words
                self.amount_in_words = number_to_words(self.amount)
            except ImportError:
                self.amount_in_words = f"{self.amount} only"
            
            # Generate QR code
            try:
                from utils.helpers import generate_qr_code
                receipt_data = {
                    'receipt_number': self.receipt_number,
                    'date': self.receipt_date.isoformat(),
                    'amount': str(self.amount),
                    'customer': self.customer.full_name,
                    'payment_method': self.payment_method,
                }
                self.qr_code = generate_qr_code(str(receipt_data))
            except ImportError:
                pass
            
            self.save()
            return True
        return False