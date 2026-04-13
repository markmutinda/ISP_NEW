from decimal import Decimal
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.conf import settings


class SMSTemplate(models.Model):
    name = models.CharField(max_length=100)
    content = models.TextField(
        help_text="Use {variable_name} for placeholders, e.g. Dear {name}, your balance is {amount}"
    )
    variables = models.JSONField(default=list, help_text="List of placeholder names")
    usage_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        app_label = 'messaging'
        ordering = ['-created_at']
        verbose_name = "SMS Template"
        verbose_name_plural = "SMS Templates"
    
    def __str__(self):
        return self.name


class SMSCampaign(models.Model):
    STATUS_CHOICES = (
        ('draft', 'Draft'),
        ('scheduled', 'Scheduled'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    )
    
    name = models.CharField(max_length=150)
    message = models.TextField()
    template = models.ForeignKey(
        SMSTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='campaigns'
    )
    recipient_filter = models.JSONField(
        default=dict,
        help_text="Filter criteria (e.g. {'status': 'active', 'plan__in': [1,2]})"
    )
    recipient_count = models.PositiveIntegerField(default=0)
    delivered_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    scheduled_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        app_label = 'messaging'
        ordering = ['-created_at']
        verbose_name = "SMS Campaign"
        verbose_name_plural = "SMS Campaigns"
    
    def __str__(self):
        return f"{self.name} ({self.status})"


class SMSMessage(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('delivered', 'Delivered'),
        ('failed', 'Failed'),
    )
    
    TYPE_CHOICES = (
        ('single', 'Single'),
        ('bulk', 'Bulk'),
        ('campaign', 'Campaign'),
        ('automated', 'Automated'),
    )
    
    recipient = models.CharField(max_length=20)  # +2547xxxxxxxx
    recipient_name = models.CharField(max_length=120, blank=True, null=True)
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sms_messages'
    )
    message = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='single')
    template = models.ForeignKey(
        SMSTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    campaign = models.ForeignKey(
        SMSCampaign,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='messages'
    )
    provider = models.CharField(max_length=50, default='africastalking')
    provider_message_id = models.CharField(max_length=100, blank=True, null=True)
    cost = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    error_message = models.TextField(blank=True, null=True)
    

    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        app_label = 'messaging'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['recipient']),
            models.Index(fields=['campaign']),
        ]
    
    def __str__(self):
        return f"{self.recipient} - {self.status}"
    
    def mark_sent(self, message_id, cost):
        self.status = 'sent'
        self.provider_message_id = message_id
        self.cost = cost
        self.sent_at = timezone.now()
        self.save(update_fields=['status', 'provider_message_id', 'cost', 'sent_at'])
    
    def mark_delivered(self):
        self.status = 'delivered'
        self.delivered_at = timezone.now()
        self.save(update_fields=['status', 'delivered_at'])
    
    def mark_failed(self, error):
        self.status = 'failed'
        self.error_message = error
        self.save(update_fields=['status', 'error_message'])


class SMSGatewayConfig(models.Model):
    """
    Per-tenant SMS gateway configuration.
    ISPs plug in their own provider credentials.
    Only one gateway can be active at a time per tenant.
    """
    PROVIDER_CHOICES = (
        ('africastalking', "Africa's Talking"),
        ('twilio', 'Twilio'),
        ('vonage', 'Vonage (Nexmo)'),
        ('infobip', 'Infobip'),
        ('beem', 'Beem Africa'),
        ('advanta', 'Advanta SMS'),
        ('hubtel', 'Hubtel'),
        ('bytewave', 'Bytewave'),
    )

    provider = models.CharField(max_length=30, choices=PROVIDER_CHOICES)
    is_active = models.BooleanField(default=False)

    # ADD THIS NEW FIELD:
    use_inbuilt_system = models.BooleanField(
        default=False, 
        help_text="Use the platform's default SMS gateway (Deducts from Tenant Wallet)"
    )

    # MODIFY THIS FIELD to allow blank keys if they use the inbuilt system
    api_key = models.CharField(max_length=255, blank=True, default='')
    api_secret = models.CharField(max_length=255, blank=True, default='')
    username = models.CharField(max_length=100, blank=True, default='')
    sender_id = models.CharField(max_length=20, blank=True, default='')

    # Provider-specific extras (e.g. account_sid for Twilio, base_url for Beem)
    extra_config = models.JSONField(default=dict, blank=True)

    # Automated triggers
    auto_payment_confirmation = models.BooleanField(default=True)
    auto_expiry_reminder = models.BooleanField(default=True)
    auto_welcome_message = models.BooleanField(default=True)
    auto_service_suspension = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'messaging'
        verbose_name = 'SMS Gateway Config'
        verbose_name_plural = 'SMS Gateway Configs'

    def __str__(self):
        if self.use_inbuilt_system:
            return f"Inbuilt System Gateway ({'active' if self.is_active else 'inactive'})"
        return f"{self.get_provider_display()} ({'active' if self.is_active else 'inactive'})"

    def save(self, *args, **kwargs):
        # Enforce: only one active gateway at a time
        if self.is_active:
            SMSGatewayConfig.objects.exclude(pk=self.pk).update(is_active=False)
        # If using inbuilt, provider field doesn't matter but set it to bytewave
        if self.use_inbuilt_system:
            self.provider = 'bytewave'
        super().save(*args, **kwargs)


class TenantSMSWallet(models.Model):
    """
    Internal SMS wallet (your own credits ledger per tenant/account in your system).
    This is what tenants buy from (M-Pesa/wallet/etc), not Bytewave directly.
    """
    sms_units = models.DecimalField(
        max_digits=14, decimal_places=4,
        default=Decimal('0.0000'),
        validators=[MinValueValidator(Decimal('0.0000'))]
    )
    sell_price_per_unit = models.DecimalField(
        max_digits=10, decimal_places=4,
        default=Decimal('0.4000'),  # your resale price, editable - updated from 0.6000 to 0.4000
        validators=[MinValueValidator(Decimal('0.0000'))]
    )
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'messaging'
        verbose_name = 'Tenant SMS Wallet'
        verbose_name_plural = 'Tenant SMS Wallets'

    def __str__(self):
        return f"Wallet(units={self.sms_units})"


class SMSCreditLedger(models.Model):
    ENTRY_TYPES = (
        ('topup', 'Topup'),
        ('debit', 'Debit'),
        ('refund', 'Refund'),
        ('adjustment', 'Adjustment'),
    )

    wallet = models.ForeignKey(TenantSMSWallet, on_delete=models.CASCADE, related_name='entries')
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPES)
    units = models.DecimalField(max_digits=14, decimal_places=4)  # positive for topup/refund, negative for debit
    unit_price = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0.0000'))
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))  # money value
    reference = models.CharField(max_length=120, blank=True, default='')
    notes = models.TextField(blank=True, default='')

    # links
    sms_message = models.ForeignKey('messaging.SMSMessage', null=True, blank=True, on_delete=models.SET_NULL)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'messaging'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.entry_type}: {self.units} units @ {self.unit_price}"


# ============================================================
# NEW MODELS ADDED BELOW
# ============================================================

class SMSNotificationSettings(models.Model):
    """
    Per-tenant SMS notification toggles for Hotspot and PPPoE/Static services.
    One row per tenant (singleton via get_or_create).
    """

    # ── INBUILT SYSTEM TOGGLE ─────────────────────────────────────────────
    use_inbuilt_system = models.BooleanField(
        default=False,
        help_text="If True, use Netily's built-in Bytewave gateway instead of custom provider"
    )

    # ── HOTSPOT NOTIFICATIONS ─────────────────────────────────────────────
    hotspot_new_subscription = models.BooleanField(default=True,
        help_text="SMS when a hotspot user makes a new purchase")
    hotspot_welcome = models.BooleanField(default=True,
        help_text="Welcome SMS when a hotspot session activates")
    hotspot_session_expiry = models.BooleanField(default=True,
        help_text="Notify hotspot user when their session is about to expire")
    hotspot_expiry_minutes_before = models.PositiveIntegerField(default=15,
        help_text="How many minutes before expiry to send the hotspot reminder")
    hotspot_payment_failed = models.BooleanField(default=True,
        help_text="Notify when hotspot STK push payment fails")
    hotspot_session_expired = models.BooleanField(default=True,
        help_text="Notify when hotspot session has fully expired")

    # ── PPPOE / STATIC NOTIFICATIONS ─────────────────────────────────────
    pppoe_welcome = models.BooleanField(default=True,
        help_text="Welcome SMS when a new PPPoE/Static customer is activated")
    pppoe_payment_confirmation = models.BooleanField(default=True,
        help_text="SMS confirmation after a PPPoE payment is received")
    pppoe_expiry_reminder = models.BooleanField(default=True,
        help_text="Remind PPPoE customer before subscription expires")
    pppoe_expiry_days_before = models.PositiveIntegerField(default=4,
        help_text="How many days before expiry to send the PPPoE reminder")
    pppoe_service_suspended = models.BooleanField(default=True,
        help_text="Notify when PPPoE service is suspended")
    pppoe_service_resumed = models.BooleanField(default=True,
        help_text="Notify when PPPoE service is restored")
    pppoe_plan_changed = models.BooleanField(default=True,
        help_text="Notify customer when their plan is changed")
    pppoe_renewal_confirmation = models.BooleanField(default=True,
        help_text="Confirm successful subscription renewal")
    pppoe_new_subscription = models.BooleanField(default=True,
        help_text="SMS when admin creates a new PPPoE subscription for a customer")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'messaging'
        verbose_name = 'SMS Notification Settings'
        verbose_name_plural = 'SMS Notification Settings'

    def __str__(self):
        return f"SMS Settings (inbuilt={self.use_inbuilt_system})"

    @classmethod
    def get_settings(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SMSUnitTopup(models.Model):
    """
    Record of SMS unit top-up purchases by a tenant.
    Units flow into TenantSMSWallet after payment confirmation.
    """
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    )

    units_purchased = models.PositiveIntegerField()
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2)
    payment_reference = models.CharField(max_length=100, blank=True)
    payment_method = models.CharField(max_length=50, default='mpesa')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    checkout_request_id = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'messaging'
        ordering = ['-created_at']
        verbose_name = 'SMS Unit Top-up'
        verbose_name_plural = 'SMS Unit Top-ups'

    def __str__(self):
        return f"{self.units_purchased} units — {self.status}"