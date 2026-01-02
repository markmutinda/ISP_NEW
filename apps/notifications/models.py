from django.db import models
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator
from apps.core.models import AuditLog

User = get_user_model()

class NotificationTemplate(models.Model):
    """Template for different types of notifications"""
    NOTIFICATION_TYPES = [
        ('sms', 'SMS'),
        ('email', 'Email'),
        ('push', 'Push Notification'),
        ('in_app', 'In-App Notification'),
        ('whatsapp', 'WhatsApp'),
    ]
    
    TRIGGER_EVENTS = [
        ('payment_received', 'Payment Received'),
        ('invoice_generated', 'Invoice Generated'),
        ('invoice_overdue', 'Invoice Overdue'),
        ('service_activation', 'Service Activation'),
        ('service_suspension', 'Service Suspension'),
        ('ticket_created', 'Ticket Created'),
        ('ticket_updated', 'Ticket Updated'),
        ('low_balance', 'Low Balance Alert'),
        ('bandwidth_limit', 'Bandwidth Limit Reached'),
        ('birthday', 'Customer Birthday'),
        ('anniversary', 'Service Anniversary'),
        ('maintenance', 'Network Maintenance'),
        ('outage', 'Network Outage'),
        ('password_reset', 'Password Reset'),
        ('welcome', 'Welcome Message'),
    ]
    
    name = models.CharField(max_length=100)
    notification_type = models.CharField(max_length=20, choices=NOTIFICATION_TYPES)
    trigger_event = models.CharField(max_length=50, choices=TRIGGER_EVENTS, unique=True)
    subject = models.CharField(max_length=200, blank=True, null=True)
    message_template = models.TextField()
    is_active = models.BooleanField(default=True)
    priority = models.IntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="1=Lowest, 5=Highest"
    )
    
    # Template variables that can be used
    available_variables = models.TextField(
        help_text="List of available template variables, comma-separated"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-priority', 'name']
        indexes = [
            models.Index(fields=['trigger_event']),
            models.Index(fields=['notification_type']),
            models.Index(fields=['is_active']),
        ]
    
    def __str__(self):
        return f"{self.name} ({self.get_notification_type_display()})"

class Notification(models.Model):
    """Actual notifications sent to users"""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('delivered', 'Delivered'),
        ('failed', 'Failed'),
        ('read', 'Read'),
    ]
    
    PRIORITY_CHOICES = [
        (1, 'Low'),
        (2, 'Medium'),
        (3, 'High'),
        (4, 'Urgent'),
        (5, 'Critical'),
    ]
    
    user = models.ForeignKey(
        User, 
        on_delete=models.CASCADE, 
        related_name='notifications',
        null=True,  # Some notifications might be system-wide
        blank=True
    )
    
    # Generic foreign key for linking to any object
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True, blank=True)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    content_object = GenericForeignKey('content_type', 'object_id')
    
    template = models.ForeignKey(
        NotificationTemplate, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True
    )
    
    notification_type = models.CharField(max_length=20, choices=NotificationTemplate.NOTIFICATION_TYPES)
    subject = models.CharField(max_length=200, blank=True, null=True)
    message = models.TextField()
    
    # Recipient details
    recipient_email = models.EmailField(blank=True, null=True)
    recipient_phone = models.CharField(max_length=15, blank=True, null=True)
    recipient_device_token = models.CharField(max_length=255, blank=True, null=True)  # For push notifications
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    priority = models.IntegerField(choices=PRIORITY_CHOICES, default=2)
    
    # Delivery tracking
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    
    # Retry logic
    retry_count = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=3)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    
    # Metadata
    metadata = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-priority', '-created_at']
        indexes = [
            models.Index(fields=['user', 'status', 'created_at']),
            models.Index(fields=['status', 'notification_type']),
            models.Index(fields=['sent_at']),
            models.Index(fields=['recipient_email', 'status']),
        ]
    
    def __str__(self):
        recipient = self.user.email if self.user else self.recipient_email or self.recipient_phone
        return f"{self.get_notification_type_display()} to {recipient} - {self.status}"
    
    def mark_as_sent(self):
        self.status = 'sent'
        self.sent_at = timezone.now()
        self.save()
    
    def mark_as_delivered(self):
        self.status = 'delivered'
        self.delivered_at = timezone.now()
        self.save()
    
    def mark_as_read(self):
        self.status = 'read'
        self.read_at = timezone.now()
        self.save()
    
    def mark_as_failed(self, error_message=""):
        self.status = 'failed'
        self.error_message = error_message
        self.save()
    
    def should_retry(self):
        return self.retry_count < self.max_retries and self.status == 'failed'

class AlertRule(models.Model):
    """Rules for automatic alert generation"""
    ALERT_TYPES = [
        ('billing', 'Billing'),
        ('network', 'Network'),
        ('customer', 'Customer'),
        ('system', 'System'),
        ('security', 'Security'),
    ]
    
    CONDITION_TYPES = [
        ('greater_than', 'Greater Than'),
        ('less_than', 'Less Than'),
        ('equals', 'Equals'),
        ('not_equals', 'Not Equals'),
        ('contains', 'Contains'),
        ('starts_with', 'Starts With'),
        ('ends_with', 'Ends With'),
    ]
    
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    alert_type = models.CharField(max_length=20, choices=ALERT_TYPES)
    
    # Condition definition
    model_name = models.CharField(
        max_length=100,
        help_text="Django model name (e.g., 'billing.Invoice')"
    )
    field_name = models.CharField(
        max_length=100,
        help_text="Field to monitor (e.g., 'amount_due')"
    )
    condition_type = models.CharField(max_length=20, choices=CONDITION_TYPES)
    condition_value = models.CharField(max_length=255)
    
    # Alert configuration
    notification_templates = models.ManyToManyField(NotificationTemplate)
    check_interval = models.IntegerField(
        default=60,
        help_text="Check interval in minutes"
    )
    is_active = models.BooleanField(default=True)
    enabled_days = models.CharField(
        max_length=100,
        default='0,1,2,3,4,5,6',
        help_text="Comma-separated days (0=Sunday, 6=Saturday)"
    )
    enabled_hours = models.CharField(
        max_length=100,
        default='0-23',
        help_text="Hour range (e.g., '8-17' for 8AM-5PM)"
    )
    
    # Cooldown to prevent spam
    cooldown_minutes = models.IntegerField(
        default=30,
        help_text="Minutes to wait before sending another alert for same condition"
    )
    
    # Target users
    target_roles = models.JSONField(
        default=list,
        help_text="List of user roles to notify"
    )
    specific_users = models.ManyToManyField(
        User,
        blank=True,
        help_text="Specific users to notify"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_checked = models.DateTimeField(null=True, blank=True)
    last_triggered = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['is_active', 'alert_type']),
        ]
    
    def __str__(self):
        return f"{self.name} ({self.get_alert_type_display()})"
    
    def is_time_valid(self):
        """Check if alert should run based on day and time"""
        now = timezone.now()
        
        # Check day
        enabled_days = [int(d) for d in self.enabled_days.split(',')]
        if now.weekday() not in enabled_days:
            return False
        
        # Check hour
        hour_range = self.enabled_hours.split('-')
        if len(hour_range) == 2:
            start_hour, end_hour = int(hour_range[0]), int(hour_range[1])
            if not (start_hour <= now.hour <= end_hour):
                return False
        
        return True

class NotificationPreference(models.Model):
    """User preferences for notifications"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='notification_preferences')
    
    # Channel preferences
    receive_email = models.BooleanField(default=True)
    receive_sms = models.BooleanField(default=True)
    receive_push = models.BooleanField(default=True)
    receive_in_app = models.BooleanField(default=True)
    
    # Category preferences
    billing_notifications = models.BooleanField(default=True)
    service_notifications = models.BooleanField(default=True)
    support_notifications = models.BooleanField(default=True)
    marketing_notifications = models.BooleanField(default=False)
    system_notifications = models.BooleanField(default=True)
    
    # Quiet hours
    quiet_hours_enabled = models.BooleanField(default=False)
    quiet_start_time = models.TimeField(default='22:00')  # 10 PM
    quiet_end_time = models.TimeField(default='07:00')    # 7 AM
    
    # Daily limit
    daily_notification_limit = models.IntegerField(default=20, help_text="Max notifications per day")
    
    # Language
    preferred_language = models.CharField(
        max_length=10,
        default='en',
        choices=[('en', 'English'), ('sw', 'Swahili')]
    )
    
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "Notification Preference"
        verbose_name_plural = "Notification Preferences"
    
    def __str__(self):
        return f"Preferences for {self.user.email}"
    
    def is_quiet_hours(self):
        """Check if current time is within quiet hours"""
        if not self.quiet_hours_enabled:
            return False
        
        now = timezone.now().time()
        if self.quiet_start_time <= self.quiet_end_time:
            return self.quiet_start_time <= now <= self.quiet_end_time
        else:
            # Crosses midnight
            return now >= self.quiet_start_time or now <= self.quiet_end_time
    
    def can_receive_notification(self, notification_type):
        """Check if user can receive notification of given type"""
        if notification_type == 'email':
            return self.receive_email
        elif notification_type == 'sms':
            return self.receive_sms
        elif notification_type == 'push':
            return self.receive_push
        elif notification_type == 'in_app':
            return self.receive_in_app
        return False

class NotificationLog(models.Model):  # Don't inherit from AuditLog
    notification = models.ForeignKey(
        Notification, 
        on_delete=models.CASCADE, 
        related_name='logs',
        null=True,
        blank=True
    )
    user = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='notification_logs'
    )
    action = models.CharField(max_length=100)  # Changed from 50 to 100 to match AuditLog
    details = models.JSONField(default=dict)
    timestamp = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    
    class Meta:
        ordering = ['-timestamp']
        verbose_name = "Notification Log"
        verbose_name_plural = "Notification Logs"
    
    def __str__(self):
        return f"{self.action} - {self.timestamp}"

class BulkNotification(models.Model):
    """For sending bulk notifications"""
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('scheduled', 'Scheduled'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]
    
    name = models.CharField(max_length=200)
    notification_type = models.CharField(max_length=20, choices=NotificationTemplate.NOTIFICATION_TYPES)
    subject = models.CharField(max_length=200)
    message = models.TextField()
    
    # Target audience
    target_segment = models.CharField(
        max_length=100,
        choices=[
            ('all_customers', 'All Customers'),
            ('active_customers', 'Active Customers'),
            ('inactive_customers', 'Inactive Customers'),
            ('overdue_customers', 'Customers with Overdue Invoices'),
            ('specific_plan', 'Specific Plan Users'),
            ('custom_list', 'Custom List'),
        ]
    )
    custom_recipients = models.JSONField(default=list, blank=True, null=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    scheduled_for = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    # Statistics
    total_recipients = models.IntegerField(default=0)
    sent_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)
    
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-scheduled_for', '-created_at']
    
    def __str__(self):
        return f"{self.name} ({self.get_status_display()})"
    
    def update_statistics(self):
        """Update sent/failed counts"""
        from .models import Notification
        stats = Notification.objects.filter(
            metadata__contains={'bulk_notification_id': self.id}
        ).aggregate(
            sent=models.Count('id', filter=models.Q(status='sent')),
            failed=models.Count('id', filter=models.Q(status='failed'))
        )
        self.sent_count = stats['sent'] or 0
        self.failed_count = stats['failed'] or 0
        self.save()