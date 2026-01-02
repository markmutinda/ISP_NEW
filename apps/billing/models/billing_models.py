from django.db import models
from django.core.validators import MinValueValidator
from django.utils import timezone
from django.db.models import Sum, Q
from decimal import Decimal
import uuid
from apps.core.models import Company
from apps.customers.models import Customer, ServiceConnection
from utils.constants import KENYAN_COUNTIES, TAX_RATES, TAX_TYPES



class Plan(models.Model):
    PLAN_TYPES = [
        ('INTERNET', 'Internet'),
        ('VOIP', 'VoIP'),
        ('IPTV', 'IPTV'),
        ('DEDICATED', 'Dedicated Line'),
        ('BUSINESS', 'Business'),
        ('RESIDENTIAL', 'Residential'),
    ]

    BILLING_CYCLES = [
        ('DAILY', 'Daily'),
        ('WEEKLY', 'Weekly'),
        ('MONTHLY', 'Monthly'),
        ('QUARTERLY', 'Quarterly'),
        ('ANNUALLY', 'Annually'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='plans')
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20, unique=True)
    plan_type = models.CharField(max_length=20, choices=PLAN_TYPES, default='INTERNET')
    description = models.TextField(blank=True)
    
    # Pricing
    base_price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    setup_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0, validators=[MinValueValidator(0)])
    tax_inclusive = models.BooleanField(default=True)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=16.0, validators=[MinValueValidator(0)])
    
    # Technical specifications
    download_speed = models.PositiveIntegerField(help_text="Speed in Mbps")
    upload_speed = models.PositiveIntegerField(help_text="Speed in Mbps")
    data_limit = models.PositiveBigIntegerField(help_text="Data limit in GB (0 for unlimited)", default=0)
    burst_limit = models.PositiveIntegerField(help_text="Burst speed in Mbps", null=True, blank=True)
    
    # Billing
    billing_cycle = models.CharField(max_length=20, choices=BILLING_CYCLES, default='MONTHLY')
    prorated_billing = models.BooleanField(default=True)
    auto_renew = models.BooleanField(default=True)
    contract_period = models.PositiveIntegerField(help_text="Contract period in months (0 for no contract)", default=0)
    early_termination_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Status
    is_active = models.BooleanField(default=True)
    is_public = models.BooleanField(default=True)
    
    # Metadata
    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='created_plans')
    updated_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='updated_plans')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['code']),
            models.Index(fields=['plan_type']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return f"{self.name} - {self.company.name}"

    @property
    def tax_amount(self):
        if self.tax_inclusive:
            return (self.base_price * self.tax_rate) / (100 + self.tax_rate)
        else:
            return (self.base_price * self.tax_rate) / 100

    @property
    def total_price(self):
        if self.tax_inclusive:
            return self.base_price
        else:
            return self.base_price + self.tax_amount

    def get_billing_cycle_days(self):
        cycle_map = {
            'DAILY': 1,
            'WEEKLY': 7,
            'MONTHLY': 30,
            'QUARTERLY': 90,
            'ANNUALLY': 365,
        }
        return cycle_map.get(self.billing_cycle, 30)


class BillingCycle(models.Model):
    CYCLE_STATUS = [
        ('OPEN', 'Open'),
        ('CLOSED', 'Closed'),
        ('PROCESSING', 'Processing'),
        ('VOIDED', 'Voided'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='billing_cycles')
    name = models.CharField(max_length=100)
    cycle_code = models.CharField(max_length=50, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    due_date = models.DateField()
    status = models.CharField(max_length=20, choices=CYCLE_STATUS, default='OPEN')
    is_locked = models.BooleanField(default=False)
    
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
        from .payment_models import Invoice
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
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='invoices')
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='invoices')
    
    # Billing Period
    billing_cycle = models.ForeignKey(BillingCycle, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    billing_date = models.DateField(default=timezone.now)
    due_date = models.DateField()
    payment_terms = models.CharField(max_length=20, choices=PAYMENT_TERMS, default='NET_15')
    
    # Service Period
    service_period_start = models.DateField()
    service_period_end = models.DateField()
    
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
    service_connection = models.ForeignKey(ServiceConnection, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    plan = models.ForeignKey(Plan, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    
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
                last_num = int(last_invoice.invoice_number.split('-')[-1])
                new_num = last_num + 1
            else:
                new_num = 1
            
            self.invoice_number = f"INV-{year_month}-{new_num:05d}"
        
        # Calculate balance
        self.balance = self.total_amount - self.amount_paid
        
        # Check if overdue
        if self.status in ['ISSUED', 'SENT', 'PARTIAL']:
            if timezone.now().date() > self.due_date:
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
            company=self.company,
            created_by=self.created_by
        )
        
        self.amount_paid += amount
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
        self.total = self.quantity * self.unit_price
        self.tax_amount = (self.total * self.tax_rate) / 100
        super().save(*args, **kwargs)
        
        # Update invoice totals
        self.invoice.calculate_totals()