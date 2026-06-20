"""
Netily Platform Subscription Models

These models live in the PUBLIC schema and handle:
1. NetilyPlan - Platform subscription tiers (Starter, Professional, Enterprise)
2. CompanySubscription - A company's active subscription
3. SubscriptionPayment - Payment records for subscriptions
4. ISPPayoutConfig - ISP's bank/M-Pesa details for receiving settlements
5. ISPSettlement - Record of payouts from Netily to ISPs
6. CommissionLedger - Track Netily's 5% commission earnings
7. BillingCycle - Tracks 30-day metered cycles for tenants
8. BillableClientRecord - Ghost records of PPPoE users counted per cycle
9. BillingSnapshot - Additional ghost record model for PPPoE users
10. TenantUserLedger - Immutable audit trail of PPPoE/Hotspot user lifecycle events
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
        ('metered', 'Metered'),
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
    
    # NEW: DYNAMIC / METERED PRICING FIELDS
    is_metered = models.BooleanField(
        default=False, 
        help_text="If True, uses dynamic metered billing instead of flat price_monthly"
    )
    base_license_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('500.00'))
    pppoe_unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('25.00'))
    pppoe_min_clients = models.PositiveIntegerField(
        default=20,
        help_text="Minimum billable PPPoE clients per cycle (floor). ISPs with fewer active clients are still billed for this many."
    )
    hotspot_revenue_share_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('3.00'))
    
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
        if self.is_metered:
            return f"{self.name} (KES {self.base_license_fee} base + usage)"
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
    TRIAL_DURATION_DAYS = 2
    
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
        if self.plan.is_metered:
            return self.plan.base_license_fee
        if self.billing_period == 'yearly':
            return self.plan.price_yearly
        return self.plan.price_monthly
    
    def extend_subscription(self, periods: int = 1):
        """
        Extend subscription, unlock the account, and generate the next Billing Cycle.
        
        This method:
        - Extends the subscription period by the specified number of billing cycles
        - Unlocks the account (sets status to 'active')
        - Marks the previous invoiced cycle as 'paid'
        - Creates the new active billing cycle for the next period
        
        This ensures that regardless of how payment is made (M-Pesa, Card, admin manual),
        the same cycle-creation logic runs and the audit trail is complete.
        """
        from django.db import transaction
        
        with transaction.atomic():
            if self.billing_period == 'yearly':
                days = 365 * periods
            else:
                days = 30 * periods
            
            # If expired/past_due, start the new cycle from right now
            if self.current_period_end < timezone.now():
                self.current_period_start = timezone.now()
                self.current_period_end = timezone.now() + timedelta(days=days)
            else:
                # Extend from current end if they paid early
                self.current_period_end += timedelta(days=days)
            
            # UNLOCK the account
            self.status = 'active'
            self.save()
            
            # ─── PHASE 4: CLOSE THE LOOP ───
            tenant = getattr(self.company, 'tenant', None)
            if not tenant and hasattr(self.company, 'tenant_set'):
                tenant = self.company.tenant_set.first()
                
            if tenant:
                # 1. Mark the previous cycle as 'paid' so it stops showing as due
                BillingCycle.objects.filter(
                    tenant=tenant,
                    subscription=self,
                    status='invoiced'
                ).update(status='paid')
                
                # 2. Generate the NEW active container for the next period
                BillingCycle.objects.get_or_create(
                    tenant=tenant,
                    subscription=self,
                    status='active',
                    defaults={
                        'start_date': self.current_period_start,
                        'end_date': self.current_period_end
                    }
                )
    
    def cancel(self, immediate: bool = False):
        """Cancel subscription"""
        self.cancelled_at = timezone.now()
        
        if immediate:
            self.status = 'cancelled'
            self.current_period_end = timezone.now()
        else:
            self.cancel_at_period_end = True
        
        self.save()
    
    def convert_from_trial(self, billing_period: str = 'monthly', defer_to_trial_end: bool = False):
        """
        Convert trial subscription to paid subscription.
        Called after successful payment.
        
        This method:
        - Converts trial to paid
        - Creates the first billing cycle (ghost container)
        - Snapshot pricing at moment of conversion
        - All wrapped in a transaction for data consistency
        
        Args:
            billing_period: 'monthly' or 'yearly'
            defer_to_trial_end: If True, keep trial active until trial_ends_at,
                then start the paid billing cycle from that date.
        """
        from django.db import transaction
        
        with transaction.atomic():
            now = timezone.now()
            
            self.billing_period = billing_period
            self.converted_from_trial_at = now
            
            if defer_to_trial_end and self.trial_ends_at and self.trial_ends_at > now:
                # DEFERRED: Payment accepted but billing starts when trial expires
                # Keep trial active, set billing cycle to start at trial end
                cycle_start = self.trial_ends_at
                self.is_trial = False
                self.status = 'active'
                
                if billing_period == 'yearly':
                    cycle_end = cycle_start + timedelta(days=365)
                else:
                    cycle_end = cycle_start + timedelta(days=30)
                
                # Period starts at trial end
                self.current_period_start = cycle_start
                self.current_period_end = cycle_end
            else:
                # IMMEDIATE: Billing starts now, trial ends immediately
                self.is_trial = False
                self.status = 'active'
                self.current_period_start = now
                
                if billing_period == 'yearly':
                    self.current_period_end = now + timedelta(days=365)
                else:
                    self.current_period_end = now + timedelta(days=30)
                
                cycle_start = now
                cycle_end = self.current_period_end
            
            self.save()
            
            # CRITICAL: Create the first billing cycle immediately
            # This ensures the ghost container exists before any usage data arrives
            tenant = getattr(self.company, 'tenant', None)
            if not tenant and hasattr(self.company, 'tenant_set'):
                tenant = self.company.tenant_set.first()
            
            if tenant:
                BillingCycle.objects.get_or_create(
                    subscription=self,
                    tenant=tenant,
                    start_date=cycle_start,
                    defaults={
                        'end_date': cycle_end,
                        'status': 'active',
                        'is_first_paid_cycle': True,  # Base fee only — metered starts next cycle
                    }
                )
    
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
        
        # Default to Metered plan for trials (pay-as-you-go, lowest barrier)
        if plan is None:
            try:
                plan = NetilyPlan.objects.get(code='metered', is_active=True)
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
    
    # The plan + billing period this payment is FOR (applied only on success)
    intended_plan = models.ForeignKey(
        NetilyPlan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='intended_payments',
        help_text="Plan to switch to upon successful payment"
    )
    intended_billing_period = models.CharField(
        max_length=20,
        choices=CompanySubscription.BILLING_PERIOD_CHOICES,
        blank=True,
        default='',
        help_text="Billing period to apply upon successful payment"
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
    # FIXED: Added unique=True to prevent duplicate webhook processing
    payhero_checkout_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
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

    # Trial payment options
    defer_billing_to_trial_end = models.BooleanField(
        default=False,
        help_text="If True, billing cycle starts when trial ends instead of now"
    )
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Subscription Payment'
        verbose_name_plural = 'Subscription Payments'
    
    def __str__(self):
        return f"{self.subscription.company.name} - KES {self.amount} ({self.status})"
    
    def mark_completed(self, mpesa_receipt: str = None):
        """
        Mark payment as completed. 
        NOTE: This is a pure state setter. It does NOT trigger subscription extensions.
        The lifecycle transition is handled explicitly by the webhook/polling views.
        """
        self.status = 'completed'
        self.completed_at = timezone.now()
        
        if mpesa_receipt:
            self.mpesa_receipt = mpesa_receipt
            
        self.save(update_fields=['status', 'completed_at', 'mpesa_receipt'])
        
        # REMOVED: self.subscription.extend_subscription()
        # Extension is now handled explicitly by the webhook to prevent double-extension
    
    def mark_failed(self, reason: str = None):
        """Mark payment as failed"""
        self.status = 'failed'
        self.failure_reason = reason
        self.save()

    def apply_intended_plan(self):
        """
        Apply the intended plan/billing period to the subscription.
        Called ONLY after payment success (webhook or polling).
        """
        if self.intended_plan:
            self.subscription.plan = self.intended_plan
        if self.intended_billing_period:
            self.subscription.billing_period = self.intended_billing_period
        if self.intended_plan or self.intended_billing_period:
            self.subscription.save(update_fields=['plan', 'billing_period'])


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


class BillingCycle(models.Model):
    """
    Tracks the 30-day metered cycle for a tenant.
    Lives in PUBLIC schema. Cannot be deleted by tenant.
    """
    
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('invoiced', 'Invoiced'),
        ('paid', 'Paid'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        'core.Tenant',
        on_delete=models.CASCADE,
        related_name='billing_cycles'
    )
    subscription = models.ForeignKey(
        CompanySubscription,
        on_delete=models.CASCADE,
        related_name='billing_cycles'
    )
    
    start_date = models.DateTimeField(default=timezone.now)
    end_date = models.DateTimeField()
    
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='active'
    )
    
    # Metered Usage
    hotspot_revenue_accumulated = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    # Track the actual generated invoice (if using apps.billing.models.Invoice)
    invoice_reference = models.CharField(max_length=100, blank=True, null=True)
    
    # ── FIX 3.1: PRICING SNAPSHOTS ──
    # Locks in the pricing at the moment the cycle is created
    snapshot_base_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    snapshot_pppoe_price = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    snapshot_min_clients = models.PositiveIntegerField(default=20, help_text="Minimum billable PPPoE clients (floor)")
    snapshot_hotspot_share_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))

    # First-paid-cycle flag: after trial conversion, this cycle charges base fee only
    is_first_paid_cycle = models.BooleanField(
        default=False,
        help_text="True for the first cycle after trial conversion. Only base license fee is charged."
    )

    # Grace period tracking
    grace_ends_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the 4-day grace period expires. Set when invoice is generated."
    )

    class Meta:
        ordering = ['-start_date']
        verbose_name = 'Billing Cycle'
        verbose_name_plural = 'Billing Cycles'

    def __str__(self):
        return f"{self.tenant.name} - Cycle {self.start_date.date()} to {self.end_date.date()}"
    
    def save(self, *args, **kwargs):
        # On creation, snapshot the plan variables
        if not self.pk and self.subscription and self.subscription.plan:
            plan = self.subscription.plan
            self.snapshot_base_fee = plan.base_license_fee
            self.snapshot_pppoe_price = plan.pppoe_unit_price
            self.snapshot_min_clients = plan.pppoe_min_clients
            self.snapshot_hotspot_share_pct = plan.hotspot_revenue_share_pct
        super().save(*args, **kwargs)

    def get_raw_pppoe_count(self):
        """
        Returns the actual number of unique physical users tracked this month.
        """
        return self.billable_clients.count()

    def calculate_total_pppoe(self):
        """
        Billable PPPoE client count for this cycle.
        Counts every PPPoE client with a footprint in the 30-day cycle.
        """
        return self.billable_clients.count()

    def calculate_pppoe_charge(self):
        """
        Calculate PPPoE charge: client footprint x unit price.
        """
        billable_count = self.calculate_total_pppoe()
        return (Decimal(str(billable_count)) * self.snapshot_pppoe_price).quantize(Decimal('0.01'))

    def refresh_actual_hotspot_revenue(self):
        """
        Reconcile and return actual hotspot revenue from paid sessions.
        """
        actual = self.get_actual_hotspot_revenue()
        if self.hotspot_revenue_accumulated != actual:
            type(self).objects.filter(pk=self.pk).update(hotspot_revenue_accumulated=actual)
            self.hotspot_revenue_accumulated = actual
        return actual

    def calculate_hotspot_revenue_share(self, revenue=None):
        """
        Netily hotspot share for this cycle.
        """
        source_revenue = self.hotspot_revenue_accumulated if revenue is None else revenue
        return (
            Decimal(str(source_revenue))
            * self.snapshot_hotspot_share_pct
            / Decimal('100.0')
        ).quantize(Decimal('0.01'))

    def calculate_usage_subtotal(self, hotspot_revenue=None):
        """
        Raw metered usage before the monthly minimum is applied.
        """
        return (
            self.calculate_pppoe_charge()
            + self.calculate_hotspot_revenue_share(hotspot_revenue)
        ).quantize(Decimal('0.01'))

    def calculate_minimum_adjustment(self, hotspot_revenue=None):
        """
        Top-up amount needed to meet the monthly minimum charge.
        """
        minimum = self.snapshot_base_fee or Decimal('500.00')
        subtotal = self.calculate_usage_subtotal(hotspot_revenue)
        return max(minimum - subtotal, Decimal('0.00')).quantize(Decimal('0.01'))

    def calculate_billable_usage_charge(self, hotspot_revenue=None):
        """
        Final metered invoice charge:
        max(PPPoE footprint + hotspot revenue share, monthly minimum).
        """
        minimum = self.snapshot_base_fee or Decimal('500.00')
        return max(self.calculate_usage_subtotal(hotspot_revenue), minimum).quantize(Decimal('0.01'))

    def calculate_hotspot_minimum_charge(self):
        """
        Backwards-compatible alias retained for older callers.
        """
        return self.calculate_billable_usage_charge()

    def calculate_total_charge(self):
        """
        Calculate recurring usage billing.

        Activation is paid once after trial. Each 30-day usage invoice is:
        max(PPPoE footprint charge + hotspot revenue share, monthly minimum).
        """
        plan = self.subscription.plan
        
        # If it's a flat-rate plan (like an old legacy plan), just return the flat price
        if not plan.is_metered:
            return self.subscription.current_price
        
        return self.calculate_billable_usage_charge()

    def get_actual_hotspot_revenue(self):
        """
        Query actual paid hotspot sessions from the tenant's schema.
        Returns the sum of amounts from sessions activated during this cycle.
        This is the source of truth — not the accumulator.
        """
        from django_tenants.utils import schema_context
        from django.db.models import Sum

        with schema_context(self.tenant.schema_name):
            from apps.billing.models.hotspot_models import HotspotSession
            result = HotspotSession.objects.filter(
                status__in=['active', 'expired'],
                activated_at__gte=self.start_date,
                activated_at__lt=self.end_date,
            ).aggregate(total=Sum('amount'))['total']
        return result or Decimal('0.00')


class BillableClientRecord(models.Model):
    """
    The 'Ghost Record' of a PPPoE user.
    Once a user connects in a billing cycle, they are recorded here permanently.
    """
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cycle = models.ForeignKey(
        BillingCycle,
        related_name='billable_clients',
        on_delete=models.CASCADE
    )
    username = models.CharField(max_length=100, db_index=True)
    first_seen = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        # A user is only counted ONCE per 30-day cycle
        unique_together = ('cycle', 'username')
        verbose_name = 'Billable Client Record'
        verbose_name_plural = 'Billable Client Records'
        indexes = [
            models.Index(fields=['cycle', 'username']),
        ]
    
    def __str__(self):
        return f"{self.username} - {self.cycle.start_date.date()}"


class BillingSnapshot(models.Model):
    """
    The 'Ghost Record' - Stores unique PPPoE users seen during a cycle.
    Even if the ISP deletes the user mid-month, this record remains for billing.
    """
    cycle = models.ForeignKey('BillingCycle', on_delete=models.CASCADE, related_name='snapshots')
    username = models.CharField(max_length=150)
    mac_address = models.CharField(max_length=50, null=True, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('cycle', 'username')
        verbose_name = "Billing Snapshot"
        verbose_name_plural = "Billing Snapshots"
        indexes = [
            models.Index(fields=['cycle', 'username']),
        ]

    def __str__(self):
        return f"{self.username} in Cycle {self.cycle_id}"


class TenantUserLedger(models.Model):
    """
    Immutable audit trail for PPPoE and Hotspot user lifecycle events.
    
    Lives in PUBLIC schema — tenants CANNOT modify or delete entries.
    Records every creation and deletion of customers/services so that
    even if a tenant deletes a user from their dashboard, the billing
    ledger retains the count for the current cycle.
    
    This also feeds into BillableClientRecord automatically: when a
    PPPoE user is created, a ghost record is inserted into the active
    billing cycle.
    """
    
    EVENT_CHOICES = [
        ('customer_created', 'Customer Created'),
        ('customer_deleted', 'Customer Deleted'),
        ('service_created', 'Service Created'),
        ('service_deleted', 'Service Deleted'),
        ('service_activated', 'Service Activated'),
        ('service_suspended', 'Service Suspended'),
        ('service_terminated', 'Service Terminated'),
    ]
    
    USER_TYPE_CHOICES = [
        ('pppoe', 'PPPoE'),
        ('hotspot', 'Hotspot'),
        ('static', 'Static IP'),
        ('dhcp', 'DHCP'),
        ('other', 'Other'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    tenant = models.ForeignKey(
        'core.Tenant',
        on_delete=models.CASCADE,
        related_name='user_ledger_entries',
    )
    
    # Event details
    event = models.CharField(max_length=30, choices=EVENT_CHOICES, db_index=True)
    user_type = models.CharField(max_length=20, choices=USER_TYPE_CHOICES, default='pppoe')
    
    # Identity snapshot (immutable — even after deletion these stay)
    customer_code = models.CharField(max_length=100, blank=True)
    customer_name = models.CharField(max_length=200, blank=True)
    username = models.CharField(
        max_length=150, blank=True, db_index=True,
        help_text="PPPoE/Hotspot username at the time of the event"
    )
    phone_number = models.CharField(max_length=20, blank=True)
    plan_name = models.CharField(max_length=200, blank=True)
    
    # Counts at the time of the event
    pppoe_count_after = models.PositiveIntegerField(
        default=0,
        help_text="Total active PPPoE services in tenant after this event"
    )
    hotspot_count_after = models.PositiveIntegerField(
        default=0,
        help_text="Total active hotspot services in tenant after this event"
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Tenant User Ledger Entry'
        verbose_name_plural = 'Tenant User Ledger'
        indexes = [
            models.Index(fields=['tenant', 'event']),
            models.Index(fields=['tenant', 'created_at']),
        ]
    
    def __str__(self):
        return f"[{self.tenant.schema_name}] {self.event} — {self.customer_name or self.username} @ {self.created_at:%Y-%m-%d %H:%M}"
    
    @classmethod
    def record(cls, tenant, event, user_type='pppoe', **kwargs):
        """
        Record an immutable ledger entry and, for PPPoE service creation,
        also insert a BillableClientRecord into the active billing cycle.
        """
        from django_tenants.utils import schema_context
        
        # Get current PPPoE and hotspot counts from tenant schema
        pppoe_count = 0
        hotspot_count = 0
        try:
            with schema_context(tenant.schema_name):
                from apps.customers.models import ServiceConnection
                pppoe_count = ServiceConnection.objects.filter(
                    status='ACTIVE', auth_connection_type='PPPOE'
                ).count()
                hotspot_count = ServiceConnection.objects.filter(
                    status='ACTIVE', auth_connection_type='HOTSPOT'
                ).count()
        except Exception:
            pass
        
        entry = cls.objects.create(
            tenant=tenant,
            event=event,
            user_type=user_type,
            pppoe_count_after=pppoe_count,
            hotspot_count_after=hotspot_count,
            **kwargs,
        )
        
        # Auto-insert BillableClientRecord for PPPoE user creation
        username = kwargs.get('username', '')
        if event == 'service_created' and user_type == 'pppoe' and username:
            try:
                active_cycle = BillingCycle.objects.filter(
                    tenant=tenant,
                    status='active',
                ).order_by('-start_date').first()
                
                if active_cycle:
                    BillableClientRecord.objects.get_or_create(
                        cycle=active_cycle,
                        username=username,
                    )
            except Exception:
                pass
        
        return entry
