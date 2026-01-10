from django.db import models
from django.core.validators import MinValueValidator
from django.utils import timezone
from decimal import Decimal
from apps.core.models import Company
#from apps.customers.models import Customer
from .billing_models import Invoice


class PaymentMethod(models.Model):
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

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='payment_methods')
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
        ]

    def __str__(self):
        payhero_status = " (PayHero)" if self.is_payhero_enabled else ""
        return f"{self.name} ({self.company.name}){payhero_status}"

    def calculate_fee(self, amount):
        if self.fee_type == 'PERCENTAGE':
            return (amount * self.transaction_fee) / 100
        return self.transaction_fee

    def is_amount_valid(self, amount):
        return self.minimum_amount <= amount <= self.maximum_amount


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
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='payments')
    customer = models.ForeignKey('customers.Customer', on_delete=models.CASCADE, related_name='payments')
    invoice = models.ForeignKey(Invoice, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')

    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0.01)])
    transaction_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default='KES')

    payment_method = models.ForeignKey(PaymentMethod, on_delete=models.PROTECT, related_name='payments')
    payment_reference = models.CharField(max_length=100, blank=True)
    transaction_id = models.CharField(max_length=100, blank=True)

    # PayHero-specific fields
    payhero_external_reference = models.CharField(max_length=255, blank=True, null=True, unique=True)
    raw_callback = models.JSONField(null=True, blank=True)

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
        ]

    def __str__(self):
        return f"Payment #{self.payment_number} - {self.customer.customer_code}"

    def save(self, *args, **kwargs):
        if not self.payment_number:
            date_str = timezone.now().strftime('%Y%m%d')
            last_payment = Payment.objects.filter(payment_number__startswith=f'PAY-{date_str}').order_by('-payment_number').first()
            new_num = int(last_payment.payment_number.split('-')[-1]) + 1 if last_payment else 1
            self.payment_number = f"PAY-{date_str}-{new_num:05d}"

        if not self.net_amount:
            self.net_amount = self.amount - self.transaction_fee

        if not self.payer_name and self.customer:
            self.payer_name = self.customer.full_name
        if not self.payer_phone and self.customer:
            self.payer_phone = self.customer.user.phone_number
        if not self.payer_email and self.customer:
            self.payer_email = self.customer.user.email

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
            company=self.company,
            customer=self.customer,
            amount=-refund_amount,
            payment_method=self.payment_method,
            status='COMPLETED',
            payment_reference=f"REFUND-{self.payment_number}",
            notes=f"Refund for {self.payment_number}. Reason: {refund_reason}",
            created_by=self.created_by
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
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='receipts')
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
            
            if last_receipt:
                last_num = int(last_receipt.receipt_number.split('-')[-1])
                new_num = last_num + 1
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
        
        super().save(*args, **kwargs)

    def issue_receipt(self, user):
        if self.status == 'DRAFT':
            self.status = 'ISSUED'
            self.issued_by = user
            self.issued_at = timezone.now()
            
            # Generate amount in words
            from utils.helpers import number_to_words
            self.amount_in_words = number_to_words(self.amount)
            
            # Generate QR code
            from utils.helpers import generate_qr_code
            receipt_data = {
                'receipt_number': self.receipt_number,
                'date': self.receipt_date.isoformat(),
                'amount': str(self.amount),
                'customer': self.customer.full_name,
                'payment_method': self.payment_method,
            }
            self.qr_code = generate_qr_code(str(receipt_data))
            
            self.save()
            return True
        return False