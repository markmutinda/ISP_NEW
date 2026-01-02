from django.db import models
from django.core.validators import MinValueValidator
from django.utils import timezone
from django.utils.crypto import get_random_string
from decimal import Decimal
import uuid
from apps.core.models import Company
from apps.customers.models import Customer
from .billing_models import Invoice

class VoucherBatch(models.Model):
    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('SUSPENDED', 'Suspended'),
        ('EXPIRED', 'Expired'),
        ('ARCHIVED', 'Archived'),
    ]

    VOUCHER_TYPES = [
        ('PREPAID', 'Prepaid Internet'),
        ('VOICE', 'Voice Calling'),
        ('DATA', 'Mobile Data'),
        ('GENERAL', 'General Purpose'),
        ('PROMOTIONAL', 'Promotional'),
        ('LOYALTY', 'Loyalty Reward'),
    ]

    # Basic Information
    batch_number = models.CharField(max_length=50, unique=True)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='voucher_batches')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    
    # Type and Value
    voucher_type = models.CharField(max_length=20, choices=VOUCHER_TYPES, default='PREPAID')
    face_value = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    sale_price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    
    # Validity
    valid_from = models.DateTimeField(default=timezone.now)
    valid_to = models.DateTimeField()
    is_reusable = models.BooleanField(default=False)
    max_uses = models.PositiveIntegerField(default=1)
    
    # Quantity
    quantity = models.PositiveIntegerField()
    issued_count = models.PositiveIntegerField(default=0)
    used_count = models.PositiveIntegerField(default=0)
    available_count = models.PositiveIntegerField(default=0)
    
    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    is_active = models.BooleanField(default=True)
    
    # Generation Settings
    prefix = models.CharField(max_length=10, default='VCH')
    length = models.PositiveIntegerField(default=12)
    charset = models.CharField(max_length=100, default='ABCDEFGHJKLMNPQRSTUVWXYZ23456789')
    
    # Restrictions
    minimum_purchase = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    customer_restriction = models.BooleanField(default=False)
    plan_restriction = models.BooleanField(default=False)
    
    # Metadata
    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='created_voucher_batches')
    approved_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_voucher_batches')
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['batch_number']),
            models.Index(fields=['status']),
            models.Index(fields=['valid_from', 'valid_to']),
        ]

    def __str__(self):
        return f"{self.name} (Batch: {self.batch_number})"

    def save(self, *args, **kwargs):
        if not self.batch_number:
            # Generate batch number: BATCH-YYYYMM-XXXX
            year_month = timezone.now().strftime('%Y%m')
            last_batch = VoucherBatch.objects.filter(
                batch_number__startswith=f'BATCH-{year_month}'
            ).order_by('-batch_number').first()
            
            if last_batch:
                last_num = int(last_batch.batch_number.split('-')[-1])
                new_num = last_num + 1
            else:
                new_num = 1
            
            self.batch_number = f"BATCH-{year_month}-{new_num:04d}"
        
        # Calculate available count
        self.available_count = self.quantity - self.issued_count
        
        # Check if expired
        if self.valid_to and timezone.now() > self.valid_to:
            self.status = 'EXPIRED'
            self.is_active = False
        
        super().save(*args, **kwargs)

    def generate_vouchers(self, count=None):
        if not count:
            count = self.quantity - self.issued_count
        
        if count <= 0 or count > (self.quantity - self.issued_count):
            return []
        
        vouchers = []
        for _ in range(count):
            # Generate unique voucher code
            while True:
                code = self.prefix + get_random_string(
                    length=self.length - len(self.prefix),
                    allowed_chars=self.charset
                )
                if not Voucher.objects.filter(code=code).exists():
                    break
            
            voucher = Voucher.objects.create(
                batch=self,
                code=code,
                face_value=self.face_value,
                sale_price=self.sale_price,
                valid_from=self.valid_from,
                valid_to=self.valid_to,
                is_reusable=self.is_reusable,
                max_uses=self.max_uses,
                status='ACTIVE',
                created_by=self.created_by
            )
            vouchers.append(voucher)
        
        self.issued_count += count
        self.save()
        
        return vouchers

    def activate_batch(self, user):
        if self.status == 'DRAFT':
            self.status = 'ACTIVE'
            self.is_active = True
            self.approved_by = user
            self.approved_at = timezone.now()
            self.save()
            return True
        return False


class Voucher(models.Model):
    VOUCHER_STATUS = [
        ('ACTIVE', 'Active'),
        ('USED', 'Used'),
        ('EXPIRED', 'Expired'),
        ('CANCELLED', 'Cancelled'),
        ('RESERVED', 'Reserved'),
    ]

    # Basic Information
    batch = models.ForeignKey(VoucherBatch, on_delete=models.CASCADE, related_name='vouchers')
    code = models.CharField(max_length=50, unique=True)
    pin = models.CharField(max_length=20, blank=True)
    
    # Value
    face_value = models.DecimalField(max_digits=10, decimal_places=2)
    sale_price = models.DecimalField(max_digits=10, decimal_places=2)
    remaining_value = models.DecimalField(max_digits=10, decimal_places=2)
    
    # Validity
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField()
    is_reusable = models.BooleanField(default=False)
    max_uses = models.PositiveIntegerField(default=1)
    use_count = models.PositiveIntegerField(default=0)
    
    # Status
    status = models.CharField(max_length=20, choices=VOUCHER_STATUS, default='ACTIVE')
    
    # Usage Tracking
    sold_to = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchased_vouchers')
    sold_at = models.DateTimeField(null=True, blank=True)
    sold_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='sold_vouchers')
    
    # Metadata
    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='created_vouchers')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['batch', 'status']),
            models.Index(fields=['valid_from', 'valid_to']),
            models.Index(fields=['sold_to']),
        ]

    def __str__(self):
        return f"Voucher {self.code} - {self.face_value} KES"

    def save(self, *args, **kwargs):
        if not self.code:
            # Generate code if not provided
            self.code = get_random_string(length=12, allowed_chars='ABCDEFGHJKLMNPQRSTUVWXYZ23456789')
        
        if not self.pin:
            # Generate PIN if not provided
            self.pin = get_random_string(length=6, allowed_chars='0123456789')
        
        # Set remaining value
        if not self.remaining_value:
            self.remaining_value = self.face_value
        
        # Check if expired
        if self.valid_to and timezone.now() > self.valid_to:
            self.status = 'EXPIRED'
        
        super().save(*args, **kwargs)

    def is_valid(self):
        now = timezone.now()
        return (
            self.status == 'ACTIVE' and
            self.valid_from <= now <= self.valid_to and
            (self.is_reusable or self.use_count < self.max_uses) and
            self.remaining_value > 0
        )

    def use_voucher(self, customer, amount, description=""):
        from .payment_models import Payment, PaymentMethod
        
        if not self.is_valid():
            return None, "Voucher is not valid"
        
        if amount > self.remaining_value:
            return None, "Insufficient voucher balance"
        
        # Create voucher usage record
        usage = VoucherUsage.objects.create(
            voucher=self,
            customer=customer,
            amount=amount,
            remaining_balance=self.remaining_value - amount,
            description=description,
            created_by=customer.user
        )
        
        # Update voucher
        self.remaining_value -= amount
        self.use_count += 1
        
        if self.remaining_value <= 0:
            self.status = 'USED'
        elif not self.is_reusable and self.use_count >= self.max_uses:
            self.status = 'USED'
        
        self.save()
        
        # Create payment record
        payment_method = PaymentMethod.objects.filter(
            method_type='VOUCHER',
            company=self.batch.company
        ).first()
        
        if payment_method:
            payment = Payment.objects.create(
                company=self.batch.company,
                customer=customer,
                amount=amount,
                payment_method=payment_method,
                payment_reference=f"VOUCHER-{self.code}",
                transaction_id=f"VCH-{usage.id}",
                status='COMPLETED',
                notes=f"Voucher payment using {self.code}",
                created_by=customer.user
            )
            
            return payment, "Voucher used successfully"
        
        return None, "Payment method not found"

    def sell_voucher(self, customer, sold_by):
        if self.sold_to:
            return False, "Voucher already sold"
        
        self.sold_to = customer
        self.sold_by = sold_by
        self.sold_at = timezone.now()
        self.save()
        
        return True, "Voucher sold successfully"


class VoucherUsage(models.Model):
    voucher = models.ForeignKey(Voucher, on_delete=models.CASCADE, related_name='usages')
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='voucher_usages')
    
    # Usage details
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    remaining_balance = models.DecimalField(max_digits=10, decimal_places=2)
    description = models.TextField(blank=True)
    
    # Reference to payment/invoice
    payment = models.ForeignKey('Payment', on_delete=models.SET_NULL, null=True, blank=True, related_name='voucher_usages')
    invoice = models.ForeignKey(Invoice, on_delete=models.SET_NULL, null=True, blank=True, related_name='voucher_usages')
    
    # Metadata
    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='created_voucher_usages')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['voucher', 'customer']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f"Voucher {self.voucher.code} used by {self.customer.customer_code}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        
        # Update invoice if provided
        if self.invoice and self.payment:
            self.invoice.add_payment(self.amount, self.payment.payment_method)