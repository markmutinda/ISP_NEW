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
    )

    provider = models.CharField(max_length=30, choices=PROVIDER_CHOICES)
    is_active = models.BooleanField(default=False)

    # Common fields
    api_key = models.CharField(max_length=255)
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
        return f"{self.get_provider_display()} ({'active' if self.is_active else 'inactive'})"