"""
Netily Platform Subscription Models

These models live in the PUBLIC schema and handle:
1. NetilyPlan - Platform subscription tiers (Starter, Professional, Enterprise)
2. CompanySubscription - A company's active subscription
3. SubscriptionPayment - Payment records for subscriptions
4. ISPPayoutConfig - ISP's bank/M-Pesa details for receiving settlements
5. ISPSettlement - Record of payouts from Netily to ISPs
6. CommissionLedger - Track Netily's 5% commission earnings
"""

import secrets
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class NetilyPlan(models.Model):
    """
    Netily platform subscription plans.
    These are the plans ISPs purchase to use the Netily platform.
    """
    
    PLAN_CODES = (
        ('starter', 'Starter'),
        ('professional', 'Professional'),
        ('enterprise', 'Enterprise'),
    )
    
    # Basic Info
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=50, unique=True, choices=PLAN_CODES)
    description = models.TextField(blank=True)
    tagline = models.CharField(max_length=255, blank=True, help_text="Short marketing tagline")
    
    # Pricing
    price_monthly = models.DecimalField(max_digits=10, decimal_places=2)
    price_yearly = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='KES')
    
    # Limits
    max_subscribers = models.PositiveIntegerField(
        help_text="Maximum number of ISP subscribers allowed. 0 = unlimited"
    )
    max_routers = models.PositiveIntegerField(
        help_text="Maximum number of routers allowed. 0 = unlimited"
    )
    max_staff = models.PositiveIntegerField(
        help_text="Maximum number of staff accounts allowed. 0 = unlimited"
    )
    
    # Features (JSON array of feature strings)
    features = models.JSONField(default=list, blank=True)
    
    # Display
    is_active = models.BooleanField(default=True)
    is_popular = models.BooleanField(default=False, help_text="Show 'Popular' badge")
    sort_order = models.PositiveIntegerField(default=0)
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['sort_order', 'price_monthly']
        verbose_name = 'Netily Plan'
        verbose_name_plural = 'Netily Plans'
    
    def __str__(self):
        return f"{self.name} (KES {self.price_monthly}/mo)"
    
    @property
    def yearly_savings(self) -> Decimal:
        """Calculate yearly savings compared to monthly billing"""
        monthly_total = self.price_monthly * 12
        return monthly_total - self.price_yearly
    
    @property
    def yearly_discount_percent(self) -> int:
        """Calculate yearly discount percentage"""
        if self.price_monthly == 0:
            return 0
        monthly_total = self.price_monthly * 12
        discount = ((monthly_total - self.price_yearly) / monthly_total) * 100
        return int(discount)


class CompanySubscription(models.Model):
    """
    A company's subscription to the Netily platform.
    Each company has one active subscription at a time.
    """
    
    STATUS_CHOICES = (
        ('active', 'Active'),
        ('past_due', 'Past Due'),
        ('cancelled', 'Cancelled'),
        ('expired', 'Expired'),
        ('trialing', 'Trial'),
    )
    
    # Trial duration in days
    TRIAL_DURATION_DAYS = 14
    
    BILLING_PERIOD_CHOICES = (
        ('monthly', 'Monthly'),
        ('yearly', 'Yearly'),
    )
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Relationships
    company = models.OneToOneField(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='subscription'
    )
    plan = models.ForeignKey(
        NetilyPlan,
        on_delete=models.PROTECT,
        related_name='subscriptions'
    )
    
    # Billing
    billing_period = models.CharField(
        max_length=20,
        choices=BILLING_PERIOD_CHOICES,
        default='monthly'
    )
    
    # Period tracking
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    
    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='trialing')
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    
    # Trial tracking
    is_trial = models.BooleanField(
        default=True,
        help_text="Whether this subscription is on free trial"
    )
    trial_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the trial started"
    )
    trial_ends_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the trial expires"
    )
    converted_from_trial_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When trial converted to paid subscription"
    )
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'Company Subscription'
        verbose_name_plural = 'Company Subscriptions'
    
    def __str__(self):
        return f"{self.company.name} - {self.plan.name} ({self.status})"
    
    @property
    def is_active(self) -> bool:
        """Check if subscription is currently active (including trial)"""
        now = timezone.now()
        
        # Active paid subscription
        if self.status == 'active' and self.current_period_end > now:
            return True
        
        # Active trial
        if self.status == 'trialing' and self.trial_ends_at and self.trial_ends_at > now:
            return True
        
        return False
    
    @property
    def is_on_trial(self) -> bool:
        """Check if currently on free trial"""
        return (
            self.is_trial and
            self.status == 'trialing' and
            self.trial_ends_at and
            self.trial_ends_at > timezone.now()
        )
    
    @property
    def trial_days_remaining(self) -> int:
        """Days remaining in trial period"""
        if not self.is_on_trial or not self.trial_ends_at:
            return 0
        delta = self.trial_ends_at - timezone.now()
        return max(0, delta.days)
    
    @property
    def trial_expired(self) -> bool:
        """Check if trial has expired without conversion"""
        return (
            self.is_trial and
            self.status == 'trialing' and
            self.trial_ends_at and
            self.trial_ends_at <= timezone.now()
        )
    
    @property
    def days_remaining(self) -> int:
        """Days remaining in current period"""
        if self.current_period_end < timezone.now():
            return 0
        delta = self.current_period_end - timezone.now()
        return delta.days
    
    @property
    def current_price(self) -> Decimal:
        """Get current price based on billing period"""
        if self.billing_period == 'yearly':
            return self.plan.price_yearly
        return self.plan.price_monthly
    
    def extend_subscription(self, periods: int = 1):
        """Extend subscription by number of periods"""
        if self.billing_period == 'yearly':
            days = 365 * periods
        else:
            days = 30 * periods
        
        # If expired, start from now
        if self.current_period_end < timezone.now():
            self.current_period_start = timezone.now()
            self.current_period_end = timezone.now() + timedelta(days=days)
        else:
            # Extend from current end
            self.current_period_end += timedelta(days=days)
        
        self.status = 'active'
        self.save()
    
    def cancel(self, immediate: bool = False):
        """Cancel subscription"""
        self.cancelled_at = timezone.now()
        
        if immediate:
            self.status = 'cancelled'
            self.current_period_end = timezone.now()
        else:
            self.cancel_at_period_end = True
        
        self.save()
    
    def convert_from_trial(self, billing_period: str = 'monthly'):
        """
        Convert trial subscription to paid subscription.
        Called after successful payment.
        """
        now = timezone.now()
        
        self.is_trial = False
        self.status = 'active'
        self.billing_period = billing_period
        self.converted_from_trial_at = now
        self.current_period_start = now
        
        # Set period end based on billing cycle
        if billing_period == 'yearly':
            self.current_period_end = now + timedelta(days=365)
        else:
            self.current_period_end = now + timedelta(days=30)
        
        self.save()
    
    @classmethod
    def create_trial_subscription(cls, company, plan=None):
        """
        Create a free trial subscription for a new company.
        
        Args:
            company: The Company instance
            plan: Optional NetilyPlan to use. Defaults to Professional plan.
        
        Returns:
            CompanySubscription instance
        """
        now = timezone.now()
        trial_end = now + timedelta(days=cls.TRIAL_DURATION_DAYS)
        
        # Default to Professional plan for trials (best features to try)
        if plan is None:
            try:
                plan = NetilyPlan.objects.get(code='professional', is_active=True)
            except NetilyPlan.DoesNotExist:
                # Fallback to any active plan
                plan = NetilyPlan.objects.filter(is_active=True).first()
                if plan is None:
                    raise ValueError("No active subscription plans available")
        
        subscription = cls.objects.create(
            company=company,
            plan=plan,
            billing_period='monthly',  # Default for trial
            status='trialing',
            is_trial=True,
            trial_started_at=now,
            trial_ends_at=trial_end,
            current_period_start=now,
            current_period_end=trial_end,  # During trial, period = trial period
        )
        
        return subscription


class SubscriptionPayment(models.Model):
    """
    Payment records for Netily platform subscriptions.
    These are payments from ISPs to Netily.
    """
    
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
        ('refunded', 'Refunded'),
    )
    
    PAYMENT_METHOD_CHOICES = (
        ('mpesa_stk', 'M-Pesa STK Push'),
        ('mpesa_paybill', 'M-Pesa Paybill'),
        ('bank_transfer', 'Bank Transfer'),
        ('card', 'Card Payment'),
    )
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Relationships
    subscription = models.ForeignKey(
        CompanySubscription,
        on_delete=models.CASCADE,
        related_name='payments'
    )
    
    # Payment Details
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='KES')
    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHOD_CHOICES,
        default='mpesa_stk'
    )
    
    # PayHero Integration
    payhero_checkout_id = models.CharField(max_length=100, blank=True, null=True)
    payhero_reference = models.CharField(max_length=100, blank=True, null=True)
    
    # M-Pesa specific
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    mpesa_receipt = models.CharField(max_length=50, blank=True, null=True)
    
    # Bank Transfer specific
    bank_reference = models.CharField(max_length=100, blank=True, null=True)
    
    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    failure_reason = models.TextField(blank=True, null=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    # Billing period this payment covers
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Subscription Payment'
        verbose_name_plural = 'Subscription Payments'
    
    def __str__(self):
        return f"{self.subscription.company.name} - KES {self.amount} ({self.status})"
    
    def mark_completed(self, mpesa_receipt: str = None):
        """Mark payment as completed and extend subscription"""
        self.status = 'completed'
        self.completed_at = timezone.now()
        
        if mpesa_receipt:
            self.mpesa_receipt = mpesa_receipt
        
        self.save()
        
        # Extend subscription
        self.subscription.extend_subscription()
    
    def mark_failed(self, reason: str = None):
        """Mark payment as failed"""
        self.status = 'failed'
        self.failure_reason = reason
        self.save()


class ISPPayoutConfig(models.Model):
    """
    ISP's bank/M-Pesa details for receiving settlements from Netily.
    This is where the ISP will receive their 95% share of customer payments.
    """
    
    PAYOUT_METHOD_CHOICES = (
        ('mpesa_b2c', 'M-Pesa (Mobile Money)'),
        ('bank_transfer', 'Bank Transfer'),
    )
    
    SETTLEMENT_FREQUENCY_CHOICES = (
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('biweekly', 'Bi-Weekly'),
        ('monthly', 'Monthly'),
    )
    
    # Kenyan Banks
    BANK_CHOICES = (
        ('kcb', 'Kenya Commercial Bank (KCB)'),
        ('equity', 'Equity Bank'),
        ('coop', 'Co-operative Bank'),
        ('stanbic', 'Stanbic Bank'),
        ('dtb', 'Diamond Trust Bank'),
        ('absa', 'ABSA Bank Kenya'),
        ('scb', 'Standard Chartered'),
        ('ncba', 'NCBA Bank'),
        ('im', 'I&M Bank'),
        ('family', 'Family Bank'),
        ('other', 'Other'),
    )
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Relationship
    company = models.OneToOneField(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='payout_config'
    )
    
    # Payout Method
    payout_method = models.CharField(
        max_length=20,
        choices=PAYOUT_METHOD_CHOICES,
        default='mpesa_b2c'
    )
    
    # M-Pesa B2C Details
    mpesa_phone = models.CharField(max_length=15, blank=True)
    mpesa_name = models.CharField(
        max_length=100, 
        blank=True,
        help_text="Verified M-Pesa registered name"
    )
    
    # Bank Details
    bank_code = models.CharField(max_length=20, choices=BANK_CHOICES, blank=True)
    bank_name = models.CharField(max_length=100, blank=True)
    bank_account_number = models.CharField(max_length=50, blank=True)
    bank_account_name = models.CharField(max_length=100, blank=True)
    bank_branch = models.CharField(max_length=100, blank=True)
    bank_swift_code = models.CharField(max_length=20, blank=True)
    
    # Verification
    is_verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_amount = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        null=True, 
        blank=True,
        help_text="Amount sent for verification"
    )
    
    # Settlement Settings
    settlement_frequency = models.CharField(
        max_length=20,
        choices=SETTLEMENT_FREQUENCY_CHOICES,
        default='weekly'
    )
    minimum_payout = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('1000.00'),
        help_text="Minimum amount before settlement is triggered"
    )
    
    # Pending Balance (unsettled amount)
    pending_balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'ISP Payout Configuration'
        verbose_name_plural = 'ISP Payout Configurations'
    
    def __str__(self):
        method = self.get_payout_method_display()
        return f"{self.company.name} - {method}"
    
    @property
    def payout_destination(self) -> str:
        """Human-readable payout destination"""
        if self.payout_method == 'mpesa_b2c':
            return f"M-Pesa: {self.mpesa_phone}"
        else:
            return f"Bank: {self.bank_name} - {self.bank_account_number[-4:].rjust(len(self.bank_account_number), '*')}"
    
    def add_to_pending_balance(self, amount: Decimal):
        """Add amount to pending balance"""
        self.pending_balance += Decimal(str(amount))
        self.save(update_fields=['pending_balance'])
    
    def clear_pending_balance(self):
        """Clear pending balance after settlement"""
        self.pending_balance = Decimal('0.00')
        self.save(update_fields=['pending_balance'])


class ISPSettlement(models.Model):
    """
    Record of settlements (payouts) from Netily to ISPs.
    After collecting customer payments, Netily pays out 95% to the ISP.
    """
    
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    )
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Relationship
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='settlements'
    )
    
    # Settlement Period
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    
    # Amounts
    gross_amount = models.DecimalField(
        max_digits=12, 
        decimal_places=2,
        help_text="Total amount collected from customers"
    )
    commission_rate = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=Decimal('0.0500'),
        help_text="Netily commission rate (default 5%)"
    )
    commission_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Netily's commission"
    )
    net_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Amount paid to ISP (gross - commission)"
    )
    
    # Payout Details
    payout_method = models.CharField(max_length=20)
    payout_destination = models.CharField(
        max_length=255,
        help_text="M-Pesa phone or bank account"
    )
    payout_reference = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="PayHero transaction reference"
    )
    
    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    failure_reason = models.TextField(blank=True, null=True)
    
    # Transaction counts
    transaction_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of customer payments in this settlement"
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'ISP Settlement'
        verbose_name_plural = 'ISP Settlements'
    
    def __str__(self):
        return f"{self.company.name} - KES {self.net_amount} ({self.status})"
    
    def mark_completed(self, payout_reference: str):
        """Mark settlement as completed"""
        self.status = 'completed'
        self.payout_reference = payout_reference
        self.processed_at = timezone.now()
        self.save()
        
        # Clear the ISP's pending balance
        try:
            payout_config = self.company.payout_config
            payout_config.clear_pending_balance()
        except ISPPayoutConfig.DoesNotExist:
            pass
    
    def mark_failed(self, reason: str):
        """Mark settlement as failed"""
        self.status = 'failed'
        self.failure_reason = reason
        self.save()


class CommissionLedger(models.Model):
    """
    Ledger tracking Netily's 5% commission from each customer payment.
    This provides a detailed audit trail of all commission earnings.
    """
    
    PAYMENT_TYPE_CHOICES = (
        ('hotspot', 'Hotspot Purchase'),
        ('recharge', 'Account Recharge'),
        ('invoice', 'Invoice Payment'),
    )
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Relationships
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='commission_entries'
    )
    settlement = models.ForeignKey(
        ISPSettlement,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='commission_entries'
    )
    
    # Payment Details
    payment_type = models.CharField(max_length=20, choices=PAYMENT_TYPE_CHOICES)
    payment_reference = models.CharField(max_length=100, help_text="Original payment reference")
    
    # Amounts
    gross_amount = models.DecimalField(max_digits=10, decimal_places=2)
    commission_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal('0.0500'))
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2)
    isp_amount = models.DecimalField(max_digits=10, decimal_places=2)
    
    # Tracking
    is_settled = models.BooleanField(default=False)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Commission Entry'
        verbose_name_plural = 'Commission Ledger'
    
    def __str__(self):
        return f"{self.company.name} - {self.payment_type} - KES {self.commission_amount}"
    
    @classmethod
    def record_commission(
        cls,
        company,
        payment_type: str,
        payment_reference: str,
        gross_amount: Decimal,
        commission_rate: Decimal = None
    ):
        """
        Record a commission entry from a customer payment.
        
        Args:
            company: The ISP company
            payment_type: Type of payment (hotspot, recharge, invoice)
            payment_reference: Unique payment reference
            gross_amount: Total payment amount
            commission_rate: Override default commission rate
            
        Returns:
            CommissionLedger instance
        """
        from django.conf import settings
        
        rate = commission_rate or Decimal(str(getattr(settings, 'NETILY_COMMISSION_RATE', 0.05)))
        gross = Decimal(str(gross_amount))
        commission = (gross * rate).quantize(Decimal('0.01'))
        isp_amount = gross - commission
        
        entry = cls.objects.create(
            company=company,
            payment_type=payment_type,
            payment_reference=payment_reference,
            gross_amount=gross,
            commission_rate=rate,
            commission_amount=commission,
            isp_amount=isp_amount,
        )
        
        # Update ISP's pending balance
        try:
            payout_config = company.payout_config
            payout_config.add_to_pending_balance(isp_amount)
        except ISPPayoutConfig.DoesNotExist:
            pass
        
        return entry
