# apps/billing/models/billing_models.py
from django.db import models
from django.core.validators import MinValueValidator
from django.utils import timezone
from django.db.models import Sum, Q
from decimal import Decimal
import uuid
from django.utils.text import slugify
from apps.core.models import Company

from utils.constants import KENYAN_COUNTIES, TAX_RATES, TAX_TYPES

class Plan(models.Model):
    PLAN_TYPE_CHOICES = [
        ('INTERNET', 'Internet'),
        ('ADDON', 'Add-on'),
        ('BUNDLE', 'Bundle'),
        ('TOPUP', 'Top-up'),
        ('PPPOE', 'PPPoE'),
        ('HOTSPOT', 'Hotspot'),
        ('STATIC', 'Static IP'),
    ]
    
    # Basic Information
    name = models.CharField(max_length=255)
    code = models.CharField(max_length=50, unique=True, blank=True)
    plan_type = models.CharField(max_length=20, choices=PLAN_TYPE_CHOICES, default='PPPOE')
    description = models.TextField(blank=True, null=True)
    
    # Pricing
    base_price = models.DecimalField(max_digits=10, decimal_places=2)  # In KES
    setup_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Speed & Data
    download_speed = models.IntegerField(null=True, blank=True)  # Mbps
    upload_speed = models.IntegerField(null=True, blank=True)    # Mbps
    data_limit = models.IntegerField(null=True, blank=True)       # GB, null = unlimited
    
    # Validity
    duration_days = models.IntegerField(default=30)  # Plan validity
    validity_hours = models.IntegerField(null=True, blank=True)  # For hourly plans
    
    # Fair Usage Policy
    fup_limit = models.IntegerField(null=True, blank=True)  # GB before throttle
    fup_speed = models.IntegerField(null=True, blank=True)  # Reduced speed in Mbps
    
    # Tenant schema field - FIXED: Removed unique=True
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        default="default_schema"
    )
    
    # Visibility & Status
    is_active = models.BooleanField(default=True)
    is_public = models.BooleanField(default=True)  # Visible in customer portal
    is_popular = models.BooleanField(default=False)  # Featured plan
    
    # Features (stored as JSON array of strings)
    features = models.JSONField(default=list, blank=True)
    
    # Metadata
    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='created_plans')
    updated_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='updated_plans')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['plan_type']),
            models.Index(fields=['is_active']),
            models.Index(fields=['is_public']),
            models.Index(fields=['is_popular']),
        ]
        verbose_name = 'Plan'
        verbose_name_plural = 'Plans'

    def __str__(self):
        return f"{self.name} ({self.code})"

    def save(self, *args, **kwargs):
        if not self.code:
            # Auto-generate code from name
            self.code = slugify(self.name).upper().replace('-', '_')
        super().save(*args, **kwargs)

    @property
    def price(self):
        """Alias for base_price for frontend compatibility"""
        return self.base_price

    @property
    def validity_days(self):
        """Alias for duration_days for frontend compatibility"""
        return self.duration_days

    @property
    def subscriber_count(self):
        # Note: Ensure service_connections is a valid related_name or exists on ServiceConnection
        if hasattr(self, 'service_connections'):
            return self.service_connections.filter(status='ACTIVE').count()
        return 0

    @property
    def subscribers_count(self):
        """Alias for subscriber_count for frontend compatibility"""
        return self.subscriber_count


class BillingCycle(models.Model):
    CYCLE_STATUS = [
        ('OPEN', 'Open'),
        ('CLOSED', 'Closed'),
        ('PROCESSING', 'Processing'),
        ('VOIDED', 'Voided'),
    ]

    name = models.CharField(max_length=100)
    cycle_code = models.CharField(max_length=50, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    due_date = models.DateField()
    status = models.CharField(max_length=20, choices=CYCLE_STATUS, default='OPEN')
    is_locked = models.BooleanField(default=False)
    
    # Tenant schema field - FIXED: Removed unique=True
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        default="default_schema"
    )
    
    # Totals
    total_invoices = models.PositiveIntegerField(default=0)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    total_paid = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    total_outstanding = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    
    # Metadata
    closed_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='closed_cycles')
    closed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='created_cycles')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_date']
        indexes = [
            models.Index(fields=['cycle_code']),
            models.Index(fields=['status']),
            models.Index(fields=['start_date', 'end_date']),
        ]

    def __str__(self):
        return f"{self.name} ({self.start_date} to {self.end_date})"

    def save(self, *args, **kwargs):
        if not self.cycle_code:
            year = self.start_date.year
            month = self.start_date.month
            self.cycle_code = f"BC-{year}-{month:02d}"
        super().save(*args, **kwargs)

    def calculate_totals(self):
        # Imported inside method to avoid circular import issues
        from .payment_models import Invoice
        # Note: Ensure Invoice model is available; Invoice is defined in this file below, 
        # but if imported from payment_models it might be different. 
        # Assuming Invoice is the one defined below in this file:
        invoices = Invoice.objects.filter(billing_cycle=self)
        
        self.total_invoices = invoices.count()
        self.total_amount = invoices.aggregate(Sum('total_amount'))['total_amount__sum'] or 0
        self.total_paid = invoices.aggregate(Sum('amount_paid'))['amount_paid__sum'] or 0
        self.total_outstanding = invoices.aggregate(Sum('balance'))['balance__sum'] or 0
        
        self.save(update_fields=['total_invoices', 'total_amount', 'total_paid', 'total_outstanding'])

    def close_cycle(self, user):
        if not self.is_locked:
            self.status = 'CLOSED'
            self.is_locked = True
            self.closed_by = user
            self.closed_at = timezone.now()
            self.save()
            return True
        return False


class Invoice(models.Model):
    INVOICE_STATUS = [
        ('DRAFT', 'Draft'),
        ('ISSUED', 'Issued'),
        ('SENT', 'Sent'),
        ('PARTIAL', 'Partially Paid'),
        ('PAID', 'Paid'),
        ('OVERDUE', 'Overdue'),
        ('VOIDED', 'Voided'),
        ('WRITTEN_OFF', 'Written Off'),
    ]

    PAYMENT_TERMS = [
        ('IMMEDIATE', 'Immediate'),
        ('NET_7', 'Net 7 Days'),
        ('NET_15', 'Net 15 Days'),
        ('NET_30', 'Net 30 Days'),
        ('DUE_ON_RECEIPT', 'Due on Receipt'),
    ]

    # Basic Information
    invoice_number = models.CharField(max_length=50, unique=True)
    customer = models.ForeignKey('customers.Customer', on_delete=models.CASCADE, related_name='invoices')
    
    # Billing Period
    billing_cycle = models.ForeignKey(BillingCycle, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    billing_date = models.DateField(default=timezone.now)
    due_date = models.DateField()
    payment_terms = models.CharField(max_length=20, choices=PAYMENT_TERMS, default='NET_15')
    
    # Service Period
    service_period_start = models.DateField()
    service_period_end = models.DateField()
    
    # Tenant schema field - FIXED: Removed unique=True
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        default="default_schema"
    )
    
    # Amounts
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # Status
    status = models.CharField(max_length=20, choices=INVOICE_STATUS, default='DRAFT')
    is_overdue = models.BooleanField(default=False)
    overdue_days = models.PositiveIntegerField(default=0)
    
    # Payment tracking
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='paid_invoices')
    
    # References
    service_connection = models.ForeignKey('customers.ServiceConnection', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    plan = models.ForeignKey('billing.Plan', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    
    # Notes
    notes = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)
    
    # Metadata
    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='created_invoices')
    issued_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='issued_invoices')
    issued_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-billing_date', '-created_at']
        indexes = [
            models.Index(fields=['invoice_number']),
            models.Index(fields=['customer', 'status']),
            models.Index(fields=['billing_date']),
            models.Index(fields=['due_date']),
            models.Index(fields=['status', 'is_overdue']),
        ]

    def __str__(self):
        return f"Invoice #{self.invoice_number} - {self.customer.customer_code}"

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            # Generate invoice number: INV-YYYYMM-XXXXX
            year_month = timezone.now().strftime('%Y%m')
            last_invoice = Invoice.objects.filter(
                invoice_number__startswith=f'INV-{year_month}'
            ).order_by('-invoice_number').first()
            
            if last_invoice:
                try:
                    last_num = int(last_invoice.invoice_number.split('-')[-1])
                    new_num = last_num + 1
                except ValueError:
                    new_num = 1
            else:
                new_num = 1
            
            self.invoice_number = f"INV-{year_month}-{new_num:05d}"
        
        # Calculate balance
        self.balance = Decimal(self.total_amount) - Decimal(self.amount_paid)
        
        # Check if overdue
        if self.status in ['ISSUED', 'SENT', 'PARTIAL']:
            if self.due_date and timezone.now().date() > self.due_date:
                self.is_overdue = True
                self.overdue_days = (timezone.now().date() - self.due_date).days
                if self.status != 'OVERDUE':
                    self.status = 'OVERDUE'
            else:
                self.is_overdue = False
                self.overdue_days = 0
        
        super().save(*args, **kwargs)

    def calculate_totals(self):
        items = self.items.all()
        self.subtotal = sum(item.total for item in items)
        self.total_amount = self.subtotal + self.tax_amount - self.discount_amount
        self.balance = self.total_amount - self.amount_paid
        self.save()

    def issue_invoice(self, user):
        if self.status == 'DRAFT':
            self.status = 'ISSUED'
            self.issued_by = user
            self.issued_at = timezone.now()
            self.save()
            return True
        return False

    def mark_as_sent(self):
        if self.status in ['ISSUED', 'DRAFT']:
            self.status = 'SENT'
            self.save()
            return True
        return False

    def add_payment(self, amount, payment_method):
        from .payment_models import Payment
        payment = Payment.objects.create(
            invoice=self,
            customer=self.customer,
            amount=amount,
            payment_method=payment_method,
            created_by=self.created_by
        )
        
        self.amount_paid += Decimal(amount)
        self.balance = self.total_amount - self.amount_paid
        
        if self.balance <= 0:
            self.status = 'PAID'
            self.paid_at = timezone.now()
        elif self.amount_paid > 0:
            self.status = 'PARTIAL'
        
        self.save()
        return payment


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items')
    description = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=16.0)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2)
    
    # Tenant schema field - FIXED: Removed unique=True
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        default="default_schema"
    )
    
    # Reference to service/plan
    service_type = models.CharField(max_length=50, blank=True)
    service_period_start = models.DateField(null=True, blank=True)
    service_period_end = models.DateField(null=True, blank=True)
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.description} - {self.invoice.invoice_number}"

    def save(self, *args, **kwargs):
        # Calculate total
        self.total = Decimal(self.quantity) * Decimal(self.unit_price)
        self.tax_amount = (self.total * Decimal(self.tax_rate)) / 100
        super().save(*args, **kwargs)
        
        # Update invoice totals
        self.invoice.calculate_totals()