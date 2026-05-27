# apps/billing/models/payment_models.py
from django.db import models
from django.db import transaction, IntegrityError  # ADDED: transaction and IntegrityError for retry loop
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
    schema_name = models.SlugField(
        max_length=63,
        editable=False
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
    
    # Callback URLs
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
            ['schema_name', 'business_shortcode', 'shortcode_type'],
        ]
        
    def __str__(self):
        env = "Sandbox" if self.is_sandbox else "Production"
        status = "✓" if self.is_active else "✗"
        default = " (Default)" if self.is_default else ""
        return f"{self.shortcode_type}: {self.business_shortcode} [{env}]{default} {status}"
    
    def clean(self):
        if self.min_transaction_amount < Decimal('0.01'):
            raise ValidationError({'min_transaction_amount': 'Minimum amount must be at least 0.01'})
            
        if self.max_transaction_amount > Decimal('150000.00'):
            raise ValidationError({'max_transaction_amount': 'Amount cannot exceed Safaricom limit of 150,000'})
            
        if self.min_transaction_amount > self.max_transaction_amount:
            raise ValidationError('Minimum amount cannot exceed maximum amount')
    
    def save(self, *args, **kwargs):
        if self.is_active:
            MpesaConfiguration.objects.filter(
                schema_name=self.schema_name, 
                is_active=True
            ).exclude(pk=self.pk).update(is_active=False)
        
        if self.is_default:
            MpesaConfiguration.objects.filter(
                schema_name=self.schema_name,
                is_default=True
            ).exclude(pk=self.pk).update(is_default=False)
        
        if not self.pk and not MpesaConfiguration.objects.filter(schema_name=self.schema_name).exists():
            self.is_default = True
            self.is_active = True
        
        self.full_clean()
        super().save(*args, **kwargs)
    
    @classmethod
    def get_active_configuration(cls, schema_name):
        return cls.objects.filter(
            schema_name=schema_name,
            is_active=True
        ).first()
    
    @classmethod
    def get_default_configuration(cls, schema_name):
        config = cls.objects.filter(
            schema_name=schema_name,
            is_default=True
        ).first()
        
        if not config:
            config = cls.get_active_configuration(schema_name)
        
        return config
    
    def get_api_environment(self):
        return "sandbox" if self.is_sandbox else "production"
    
    def get_callback_url(self, request=None):
        """
        Default C2B Callback URL for M-Pesa transactions.
        NOTE: This is specifically for C2B (Customer to Business) webhook callbacks.
        For STK Push callbacks, use a different endpoint.
        UPDATED: Changed from 'mpesa/c2b-callback/' to 'daraja/c2b-callback/'
        """
        if self.callback_url:
            return self.callback_url

        sub_domain = self.schema_name.replace('tenant_', '')
        # FIX: Updated to use new daraja path instead of mpesa path
        return f"https://{sub_domain}.netily.co.ke/api/v1/billing/daraja/c2b-callback/"
    
    def get_validation_url(self, request=None):
        """
        C2B Validation URL for M-Pesa transactions.
        This endpoint receives validation requests from Safaricom before a transaction is completed.
        The validation URL is separate from the callback URL and is used to validate
        transactions before they are processed.
        """
        if self.callback_url:
            # If a custom callback URL is set, append /validate/ to it for validation
            # This assumes the custom URL base can be used for both callback and validation
            return self.callback_url.rstrip('/') + '/validate/'
        
        sub_domain = self.schema_name.replace('tenant_', '')
        # Return the validation endpoint URL matching the new daraja validation route
        return f"https://{sub_domain}.netily.co.ke/api/v1/billing/daraja/c2b-validation/"
    
    def get_timeout_url(self, request=None):
        if self.timeout_url:
            return self.timeout_url
        
        sub_domain = self.schema_name.replace('tenant_', '')
        return f"https://{sub_domain}.netily.co.ke/api/v1/billing/mpesa/timeout/"


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
    
    payment = models.OneToOneField(
        'billing.Payment', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='payment_log'
    )
    
    configuration = models.ForeignKey(
        'billing.MpesaConfiguration', 
        on_delete=models.PROTECT,
        related_name='transactions'
    )
    
    schema_name = models.SlugField(max_length=63, editable=False)
    
    merchant_request_id = models.CharField(max_length=100, unique=True)
    checkout_request_id = models.CharField(max_length=100, unique=True)
    
    # 🧠 Widen transaction_id to 255 to accommodate long M-Pesa receipt numbers
    transaction_id = models.CharField(
        max_length=255, 
        blank=True, 
        null=True,  # 🧠 Allow null values for pending STK pushes
        db_index=True, 
        unique=True,
        help_text="M-Pesa Receipt Number (unique)"
    )
    
    # 🧠 Increase these fields to max_length=255 to prevent StringDataRightTruncation errors
    transaction_type = models.CharField(max_length=255, choices=TRANSACTION_TYPE, default='STK_PUSH')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    phone_number = models.CharField(max_length=255)
    account_reference = models.CharField(max_length=255, blank=True)
    transaction_desc = models.CharField(max_length=200, blank=True)
    
    status = models.CharField(max_length=255, choices=TRANSACTION_STATUS, default='PENDING')
    result_code = models.IntegerField(null=True, blank=True)
    result_desc = models.TextField(blank=True)
    
    callback_data = models.JSONField(null=True, blank=True)
    callback_received_at = models.DateTimeField(null=True, blank=True)
    
    request_payload = models.JSONField(null=True, blank=True)
    response_payload = models.JSONField(null=True, blank=True)
    
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
        # Convert empty strings to None so the unique constraint allows multiple pending checkouts
        if not self.transaction_id:
            self.transaction_id = None

        if not self.schema_name and self.configuration:
            self.schema_name = self.configuration.schema_name
        
        if not self.schema_name:
            from django.core.exceptions import ValidationError
            raise ValidationError("schema_name must be set for MpesaTransaction")
            
        super().save(*args, **kwargs)
    
    def mark_completed(self, transaction_id, callback_data=None):
        self.status = 'COMPLETED'
        self.transaction_id = transaction_id
        self.result_code = 0
        self.result_desc = "Success"
        if callback_data:
            self.callback_data = callback_data
            self.callback_received_at = timezone.now()
        self.save()
    
    def mark_failed(self, result_code, result_desc, callback_data=None):
        self.status = 'FAILED'
        self.result_code = result_code
        self.result_desc = result_desc
        if callback_data:
            self.callback_data = callback_data
            self.callback_received_at = timezone.now()
        self.save()
    
    def mark_timeout(self):
        self.status = 'TIMEOUT'
        self.result_desc = "Transaction timed out - no callback received"
        self.save()


class StkCancellationTracker(models.Model):
    """
    Tracks consecutive STK Push cancellations (result_code 1032) per tenant + phone number
    to prevent abuse (users repeatedly cancelling STK pushes).
    """
    schema_name = models.SlugField(max_length=63, db_index=True)
    phone_number = models.CharField(max_length=20, db_index=True)
    
    consecutive_1032_count = models.PositiveIntegerField(default=0)
    is_blocked = models.BooleanField(default=False)
    blocked_at = models.DateTimeField(null=True, blank=True)
    
    last_result_code = models.IntegerField(null=True, blank=True)
    last_checkout_request_id = models.CharField(max_length=120, blank=True)
    
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'STK Cancellation Tracker'
        verbose_name_plural = 'STK Cancellation Trackers'
        unique_together = [('schema_name', 'phone_number')]
        indexes = [
            models.Index(fields=['schema_name', 'phone_number']),
            models.Index(fields=['schema_name', 'is_blocked']),
            models.Index(fields=['schema_name', 'consecutive_1032_count']),
        ]

    def __str__(self):
        status = "BLOCKED" if self.is_blocked else f"{self.consecutive_1032_count} cancellations"
        return f"{self.phone_number} [{self.schema_name}] - {status}"

    @classmethod
    def get_or_create_tracker(cls, schema_name, phone_number):
        tracker, created = cls.objects.get_or_create(
            schema_name=schema_name,
            phone_number=phone_number
        )
        return tracker

    def record_result_code(self, result_code: int, checkout_request_id: str = ""):
        self.last_result_code = result_code
        self.last_checkout_request_id = checkout_request_id
        self.updated_at = timezone.now()

        if result_code == 1032:
            self.consecutive_1032_count += 1
            if self.consecutive_1032_count >= 3 and not self.is_blocked:
                self.is_blocked = True
                self.blocked_at = timezone.now()
        else:
            if result_code in [0, 1037]:
                self.consecutive_1032_count = 0
                self.is_blocked = False
                self.blocked_at = None
            else:
                self.consecutive_1032_count = 0

        self.save()
        return self

    def is_currently_blocked(self) -> bool:
        """Check if this phone is currently blocked from STK Push"""
        if not self.is_blocked:
            return False
        # No auto-unblock — block persists until manual reset
        return True

    def reset(self):
        """Manually reset tracker (e.g. after admin intervention)"""
        self.consecutive_1032_count = 0
        self.is_blocked = False
        self.blocked_at = None
        self.last_result_code = None
        self.last_checkout_request_id = ""
        self.save()


class TenantTumaConfig(models.Model):
    """
    Tenant-specific Tuma (payment gateway) configuration.
    Supports Till and Bank payment modes exclusively.
    
    🔧 FIX: Removed the ForeignKey to Tenant to prevent cross-schema deletion crashes.
    Now relying entirely on schema_name for tenant identification.
    """
    MODE_CHOICES = [
        ("TILL", "Till"),
        ("BANK", "Bank"),
    ]

    schema_name = models.SlugField(max_length=63, unique=True, db_index=True)
    
    # ❌ REMOVED: ForeignKey to Tenant was causing cascade deletion crashes
    # The schema_name field is now the canonical owner key
    # tenant = models.ForeignKey(
    #     "core.Tenant", 
    #     on_delete=models.CASCADE, 
    #     related_name="tuma_configs",
    #     null=True,
    #     blank=True
    # )

    tuma_business_id = models.CharField(max_length=64, blank=True)
    tuma_business_email = models.EmailField(blank=True)
    tuma_business_api_key = models.CharField(max_length=255, blank=True)

    active_mode = models.CharField(max_length=10, choices=MODE_CHOICES, blank=True)

    collection_reference_id = models.CharField(
        max_length=64, 
        blank=True, 
        db_index=True,
        help_text="Tuma reference ID for the selected bank/till/paybill"
    )
    collection_reference_code = models.CharField(
        max_length=30, 
        blank=True,
        help_text="Reference code (e.g., BUYGOODS, PAYBILL, EQUITY, TILL_NUMBER)"
    )
    collection_reference_name = models.CharField(
        max_length=120, 
        blank=True,
        help_text="Display name of the selected bank or payment method"
    )
    collection_account_number = models.CharField(
        max_length=50, 
        blank=True,
        help_text="Account number (till number, paybill number, or bank account)"
    )

    # DEPRECATED fields kept for backward compatibility
    till_number = models.CharField(max_length=30, blank=True)
    bank_id = models.CharField(max_length=64, blank=True)
    bank_name = models.CharField(max_length=100, blank=True)
    bank_account_number = models.CharField(max_length=50, blank=True)

    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Tuma Configuration'
        verbose_name_plural = 'Tuma Configurations'
        indexes = [
            models.Index(fields=["schema_name", "is_active"]),
            models.Index(fields=["collection_reference_id"]),
            models.Index(fields=["collection_reference_code"]),
        ]

    def __str__(self):
        mode = self.active_mode or "Not configured"
        ref_display = f" - {self.collection_reference_name}" if self.collection_reference_name else ""
        return f"Tuma Config - {self.schema_name} ({mode}){ref_display}"

    def clean_mode(self):
        if self.active_mode == "TILL":
            if not self.collection_reference_id or not self.collection_account_number:
                raise ValidationError({
                    'collection_reference_id': 'Reference is required when mode is TILL',
                    'collection_account_number': 'Account number is required when mode is TILL'
                })
            self.bank_id = ""
            self.bank_name = ""
            self.bank_account_number = ""
            self.till_number = self.collection_account_number
            
        elif self.active_mode == "BANK":
            if not self.collection_reference_id or not self.collection_account_number:
                raise ValidationError({
                    'collection_reference_id': 'Bank selection is required when mode is BANK',
                    'collection_account_number': 'Account number is required when mode is BANK'
                })
            self.till_number = ""
            self.bank_id = self.collection_reference_id
            self.bank_name = self.collection_reference_name
            self.bank_account_number = self.collection_account_number
            
        elif self.active_mode == "":
            self.collection_reference_id = ""
            self.collection_reference_code = ""
            self.collection_reference_name = ""
            self.collection_account_number = ""
            self.till_number = ""
            self.bank_id = ""
            self.bank_name = ""
            self.bank_account_number = ""

    def clean(self):
        self.clean_mode()
        
    def save(self, *args, **kwargs):
        if self.active_mode == "TILL" and self.till_number and not self.collection_account_number:
            self.collection_account_number = self.till_number
        if self.active_mode == "BANK" and self.bank_id and not self.collection_reference_id:
            self.collection_reference_id = self.bank_id
            self.collection_reference_name = self.bank_name
            self.collection_account_number = self.bank_account_number
            
        self.full_clean()
        super().save(*args, **kwargs)
    
    def get_collection_display(self):
        if not self.active_mode:
            return None
            
        return {
            'mode': self.active_mode,
            'reference_id': self.collection_reference_id,
            'reference_code': self.collection_reference_code,
            'reference_name': self.collection_reference_name,
            'account_number': self.collection_account_number,
            'display': f"{self.collection_reference_name} - {self.collection_account_number}" if self.collection_reference_name else self.collection_account_number
        }


# ==================== InvoiceItemPayment Model (UPDATED - PayHero Removed) ====================

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
    # 🧠 Widened to 100 to support long auto-generated webhook codes
    code = models.CharField(max_length=100, unique=True)
    method_type = models.CharField(max_length=20, choices=METHOD_TYPES)
    description = models.TextField(blank=True)

    # REMOVED: channel_id = models.IntegerField(null=True, blank=True)
    # REMOVED: is_payhero_enabled = models.BooleanField(default=False)
    
    till_number = models.CharField(max_length=20, null=True, blank=True)
    paybill_number = models.CharField(max_length=20, null=True, blank=True)
    # 🧠 Widened to 255 to prevent truncation errors
    account_number = models.CharField(max_length=255, null=True, blank=True)
    bank_name = models.CharField(max_length=100, null=True, blank=True)
    custom_link = models.URLField(null=True, blank=True)
    is_default = models.BooleanField(default=False)

    mpesa_configuration = models.ForeignKey(
        'billing.MpesaConfiguration',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payment_methods'
    )

    tuma_configuration = models.ForeignKey(
        'billing.TenantTumaConfig',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payment_methods'
    )

    is_active = models.BooleanField(default=True)
    requires_confirmation = models.BooleanField(default=False)
    confirmation_timeout = models.PositiveIntegerField(help_text="Timeout in minutes", default=30)

    transaction_fee = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    fee_type = models.CharField(max_length=10, choices=[('PERCENTAGE', 'Percentage'), ('FIXED', 'Fixed')], default='FIXED')

    minimum_amount = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    maximum_amount = models.DecimalField(max_digits=10, decimal_places=2, default=1000000)

    integration_class = models.CharField(max_length=100, blank=True)
    config_json = models.JSONField(default=dict, blank=True)

    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        default="default_schema"
    )
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    last_used = models.DateTimeField(null=True, blank=True)

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
            # REMOVED: models.Index(fields=['channel_id']),
            models.Index(fields=['is_default']),
            models.Index(fields=['schema_name', 'method_type']),
        ]

    def __str__(self):
        # REMOVED: payhero_status = " (PayHero)" if self.is_payhero_enabled else ""
        mpesa_status = " (M-Pesa)" if self.mpesa_configuration else ""
        tuma_status = " (Tuma)" if self.tuma_configuration else ""
        return f"{self.name}{mpesa_status}{tuma_status}"

    def calculate_fee(self, amount):
        if self.fee_type == 'PERCENTAGE':
            return (amount * self.transaction_fee) / 100
        return self.transaction_fee

    def is_amount_valid(self, amount):
        return self.minimum_amount <= amount <= self.maximum_amount
    
    def get_mpesa_config(self):
        if self.mpesa_configuration and self.mpesa_configuration.is_active:
            return self.mpesa_configuration
        return MpesaConfiguration.get_default_configuration(self.schema_name)
    
    def get_tuma_config(self):
        if self.tuma_configuration and self.tuma_configuration.is_active:
            return self.tuma_configuration
        return None


# ==================== Payment Model (UPDATED - WITH RETRY LOOP) ====================

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

    # 🧠 Widen these fields to 255 to prevent truncation
    payment_number = models.CharField(max_length=255, unique=True)
    
    # FIX: Made customer field optional for Hotspot-only payments
    # Using SET_NULL to preserve payment history even if customer is deleted
    customer = models.ForeignKey(
        'customers.Customer', 
        on_delete=models.SET_NULL,  # Changed from CASCADE to SET_NULL
        null=True,
        blank=True,
        related_name='payments',
        help_text="Customer linked to this payment. Set null if customer is deleted to preserve transaction history."
    )
    invoice = models.ForeignKey(Invoice, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')

    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0.01)])
    transaction_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default='KES')

    # 🔧 FIX: Changed from PROTECT to SET_NULL to allow payment method deletion while preserving payment history
    payment_method = models.ForeignKey(
        'billing.InvoiceItemPayment', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='payments',
        help_text="Set to null if the payment method is deleted to preserve history"
    )
    payment_reference = models.CharField(max_length=255, blank=True)
    transaction_id = models.CharField(max_length=255, blank=True)

    # NEW FIELD: Explicit link to HotspotSession for STK payments
    hotspot_session = models.ForeignKey(
        'billing.HotspotSession',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payments',
        db_index=True,
        help_text="Explicit link to the hotspot session that initiated this payment (for STK Push)"
    )

    # REMOVED: payhero_external_reference = models.CharField(max_length=255, blank=True, null=True, unique=True)
    # REMOVED: raw_callback = models.JSONField(null=True, blank=True)
    
    mpesa_transaction = models.OneToOneField(
        'billing.MpesaTransaction',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payment_record'
    )

    tuma_merchant_request_id = models.CharField(max_length=120, blank=True, db_index=True)
    tuma_checkout_request_id = models.CharField(max_length=120, blank=True, db_index=True)
    tuma_status = models.CharField(max_length=50, blank=True)  # Already widened from 20 to 50
    tuma_result_code = models.IntegerField(null=True, blank=True)
    tuma_result_desc = models.TextField(blank=True)
    tuma_callback_payload = models.JSONField(null=True, blank=True)

    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        default="default_schema"
    )
    
    status = models.CharField(max_length=50, choices=PAYMENT_STATUS, default='PENDING')  # Already widened from 20 to 50
    is_reconciled = models.BooleanField(default=False)

    # 🔧 FIX: Changed from timezone.now to timezone.now (callable) for naive datetime warning fix
    payment_date = models.DateTimeField(default=timezone.now)  # This is now a callable, not a fixed value
    processed_at = models.DateTimeField(null=True, blank=True)
    reconciled_at = models.DateTimeField(null=True, blank=True)

    payer_name = models.CharField(max_length=200, blank=True)
    payer_phone = models.CharField(max_length=50, blank=True)  # Already widened from 20 to 50
    payer_email = models.EmailField(blank=True)
    payer_id_number = models.CharField(max_length=50, blank=True)

    bank_name = models.CharField(max_length=100, blank=True)
    account_number = models.CharField(max_length=255, blank=True)
    branch = models.CharField(max_length=100, blank=True)
    cheque_number = models.CharField(max_length=255, blank=True)

    mpesa_receipt = models.CharField(max_length=255, blank=True)
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
            # REMOVED: models.Index(fields=['payhero_external_reference']),
            models.Index(fields=['tuma_merchant_request_id']),
            models.Index(fields=['tuma_checkout_request_id']),
            models.Index(fields=['hotspot_session']),  # Added index for the new field
            models.Index(fields=['schema_name', 'status', 'payment_date']),
        ]

    def __str__(self):
        customer_ref = self.customer.customer_code if self.customer else "Hotspot Client"
        return f"Payment #{self.payment_number} - {customer_ref}"

    def save(self, *args, **kwargs):
        # 1. Handle standard fields first
        if self.net_amount == 0 and self.amount:
            self.net_amount = self.amount - self.transaction_fee
        
        if not self.schema_name and self.customer:
            self.schema_name = self.customer.schema_name
            
        # 2. Handle ID Generation with Concurrency Protection (RETRY LOOP)
        if not self.payment_number:
            year = timezone.now().year
            month = timezone.now().month
            prefix = f'PAY-{year}-{month:02d}-'
            
            last_payment = Payment.objects.filter(
                payment_number__startswith=prefix
            ).order_by('-payment_number').first()
            
            if last_payment and last_payment.payment_number:
                try:
                    last_num = int(last_payment.payment_number.split('-')[-1])
                    new_num = last_num + 1
                except (IndexError, ValueError):
                    new_num = 1
            else:
                new_num = 1
            
            # Retry loop: If another transaction steals the number, increment and try again
            while True:
                self.payment_number = f"{prefix}{new_num:05d}"
                try:
                    # Nested atomic block prevents the main transaction from aborting on error
                    with transaction.atomic():
                        super().save(*args, **kwargs)
                    break  # Success! Exit the loop.
                except IntegrityError as e:
                    if 'payment_number' in str(e) or 'payment_number_key' in str(e):
                        new_num += 1  # Number was taken, try the next one
                    else:
                        raise  # Re-raise if it's a completely different database error
        else:
            # Normal save for existing objects
            super().save(*args, **kwargs)
    
    def mark_as_completed(self, transaction_id=None, receipt_number=None, processed_by=None):
        """Mark payment as completed"""
        self.status = 'COMPLETED'
        self.processed_at = timezone.now()
        
        if transaction_id:
            self.transaction_id = transaction_id
        
        if processed_by:
            self.processed_by = processed_by
        
        self.save()
        
        if self.invoice:
            from .billing_models import Invoice
            Invoice.objects.filter(pk=self.invoice.pk).update(
                paid_amount=models.F('paid_amount') + self.net_amount
            )
    
    def mark_as_failed(self, failure_reason, processed_by=None):
        """Mark payment as failed"""
        self.status = 'FAILED'
        self.failure_reason = failure_reason
        
        if processed_by:
            self.processed_by = processed_by
        
        self.save()
    
    def refund(self, amount=None, reason=None, refunded_by=None):
        """Process refund for this payment"""
        if self.status != 'COMPLETED':
            raise ValidationError("Only completed payments can be refunded")
        
        refund_amount = amount or self.net_amount
        
        if refund_amount > self.net_amount:
            raise ValidationError("Refund amount cannot exceed paid amount")
        
        from .billing_models import PaymentRefund
        refund = PaymentRefund.objects.create(
            payment=self,
            amount=refund_amount,
            reason=reason,
            refunded_by=refunded_by,
            refund_date=timezone.now(),
            status='PROCESSED'
        )
        
        if refund_amount == self.net_amount:
            self.status = 'REFUNDED'
        else:
            self.status = 'PARTIALLY_REFUNDED'
        
        self.save()
        
        return refund


class Receipt(models.Model):
    RECEIPT_STATUS = [
        ('DRAFT', 'Draft'),
        ('ISSUED', 'Issued'),
        ('CANCELLED', 'Cancelled'),
    ]

    receipt_number = models.CharField(max_length=50, unique=True)
    
    # FIX: Made customer field optional for Hotspot-only receipts
    customer = models.ForeignKey(
        'customers.Customer', 
        on_delete=models.SET_NULL,  # Changed from CASCADE to SET_NULL
        null=True,
        blank=True,
        related_name='receipts',
        help_text="Customer linked to this receipt. Set null if customer is deleted to preserve receipt history."
    )
    
    payment = models.OneToOneField(Payment, on_delete=models.CASCADE, related_name='receipt')
    
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    amount_in_words = models.CharField(max_length=500)
    currency = models.CharField(max_length=3, default='KES')
    
    payment_method = models.CharField(max_length=100)
    payment_reference = models.CharField(max_length=100, blank=True)
    
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        default="default_schema"
    )
    
    status = models.CharField(max_length=20, choices=RECEIPT_STATUS, default='DRAFT')
    
    receipt_date = models.DateTimeField(default=timezone.now)
    issued_at = models.DateTimeField(null=True, blank=True)
    
    issued_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='issued_receipts')
    
    notes = models.TextField(blank=True)
    
    digital_signature = models.TextField(blank=True)
    qr_code = models.TextField(blank=True)
    
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
        customer_ref = self.customer.customer_code if self.customer else "Hotspot Client"
        return f"Receipt #{self.receipt_number} - {customer_ref}"

    def save(self, *args, **kwargs):
        # 1. Handle standard fields first
        if not self.amount and self.payment:
            self.amount = self.payment.amount
        
        if not self.payment_method and self.payment:
            self.payment_method = self.payment.payment_method.name if self.payment.payment_method else "Unknown"
        
        if not self.payment_reference and self.payment:
            self.payment_reference = self.payment.payment_reference
        
        if not self.schema_name and self.payment:
            self.schema_name = self.payment.schema_name

        # 2. Handle ID Generation with Concurrency Protection (RETRY LOOP)
        if not self.receipt_number:
            year = timezone.now().year
            prefix = f'RCPT-{year}-'
            
            last_receipt = Receipt.objects.filter(
                receipt_number__startswith=prefix
            ).order_by('-receipt_number').first()
            
            if last_receipt and last_receipt.receipt_number:
                try:
                    last_num = int(last_receipt.receipt_number.split('-')[-1])
                    new_num = last_num + 1
                except (IndexError, ValueError):
                    new_num = 1
            else:
                new_num = 1
            
            # Retry loop: If another transaction steals the number, increment and try again
            while True:
                self.receipt_number = f"{prefix}{new_num:05d}"
                try:
                    # Nested atomic block prevents the main transaction from aborting on error
                    with transaction.atomic():
                        super().save(*args, **kwargs)
                    break  # Success! Exit the loop.
                except IntegrityError as e:
                    if 'receipt_number' in str(e) or 'receipt_number_key' in str(e):
                        new_num += 1  # Number was taken, try the next one
                    else:
                        raise  # Re-raise if it's a completely different database error
        else:
            # Normal save for existing objects
            super().save(*args, **kwargs)

    def issue_receipt(self, user):
        if self.status == 'DRAFT':
            self.status = 'ISSUED'
            self.issued_by = user
            self.issued_at = timezone.now()
            
            try:
                from utils.helpers import number_to_words
                self.amount_in_words = number_to_words(self.amount)
            except ImportError:
                self.amount_in_words = f"{self.amount} only"
            
            try:
                from utils.helpers import generate_qr_code
                customer_name = self.customer.full_name if self.customer else "Hotspot Client"
                receipt_data = {
                    'receipt_number': self.receipt_number,
                    'date': self.receipt_date.isoformat(),
                    'amount': str(self.amount),
                    'customer': customer_name,
                    'payment_method': self.payment_method,
                }
                self.qr_code = generate_qr_code(str(receipt_data))
            except ImportError:
                pass
            
            self.save()
            return True
        return False