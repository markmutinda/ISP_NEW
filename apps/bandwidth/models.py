from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from django.conf import settings



# Use settings.AUTH_USER_MODEL for User references
User = settings.AUTH_USER_MODEL


class BandwidthProfile(models.Model):
    """Bandwidth profiles/tiers for customers"""
    PROFILE_TYPES = [
        ('residential', 'Residential'),
        ('business', 'Business'),
        ('enterprise', 'Enterprise'),
        ('hotspot', 'Hotspot'),
    ]
    
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    profile_type = models.CharField(max_length=20, choices=PROFILE_TYPES, default='residential')
    
    download_speed = models.PositiveIntegerField(help_text="Download speed in Mbps")
    upload_speed = models.PositiveIntegerField(help_text="Upload speed in Mbps")
    
    data_cap = models.PositiveIntegerField(default=0, help_text="Monthly data cap in GB (0 for unlimited)")
    burst_limit = models.PositiveIntegerField(default=0, help_text="Burst limit in Mbps (0 for no burst)")
    burst_duration = models.PositiveIntegerField(default=0, help_text="Burst duration in seconds")
    
    priority = models.IntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(10)])
    guaranteed_min_speed = models.PositiveIntegerField(default=0, help_text="Guaranteed minimum speed in Mbps")
    
    peak_hours_only = models.BooleanField(default=False)
    peak_start_time = models.TimeField(null=True, blank=True)
    peak_end_time = models.TimeField(null=True, blank=True)
    
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # TenantMixin required field with default
    class Meta:
        app_label = 'bandwidth'
        ordering = ['priority', 'monthly_price']
        verbose_name = "Bandwidth Profile"
        verbose_name_plural = "Bandwidth Profiles"
    
    def __str__(self):
        return f"{self.name} ({self.download_speed}/{self.upload_speed} Mbps)"


class TrafficRule(models.Model):
    """Traffic shaping and filtering rules"""
    RULE_TYPES = [
        ('limit', 'Bandwidth Limit'),
        ('queue', 'Queue Rule'),
        ('filter', 'Packet Filter'),
        ('nat', 'NAT Rule'),
        ('mangle', 'Mangle Rule'),
    ]
    
    RULE_PRIORITIES = [
        ('critical', 'Critical'),
        ('high', 'High'),
        ('medium', 'Medium'),
        ('low', 'Low'),
    ]
    
    name = models.CharField(max_length=100)
    rule_type = models.CharField(max_length=20, choices=RULE_TYPES)
    priority_level = models.CharField(max_length=20, choices=RULE_PRIORITIES, default='medium')
    
    source_ip = models.GenericIPAddressField(null=True, blank=True, help_text="Source IP address")
    destination_ip = models.GenericIPAddressField(null=True, blank=True, help_text="Destination IP address")
    source_port = models.PositiveIntegerField(null=True, blank=True)
    destination_port = models.PositiveIntegerField(null=True, blank=True)
    protocol = models.CharField(max_length=10, choices=[('tcp', 'TCP'), ('udp', 'UDP'), ('icmp', 'ICMP'), ('any', 'Any')], default='any')
    
    max_bandwidth = models.PositiveIntegerField(help_text="Maximum bandwidth in Kbps")
    guaranteed_bandwidth = models.PositiveIntegerField(default=0, help_text="Guaranteed bandwidth in Kbps")
    
    schedule_enabled = models.BooleanField(default=False)
    schedule_start = models.TimeField(null=True, blank=True)
    schedule_end = models.TimeField(null=True, blank=True)
    schedule_days = models.CharField(max_length=50, default='mon,tue,wed,thu,fri,sat,sun')
    
    application_protocol = models.CharField(max_length=50, blank=True)
    content_filter = models.TextField(blank=True)
    
    target_device_id = models.PositiveIntegerField(null=True, blank=True)
    target_device_type = models.CharField(max_length=50, blank=True)
    
    is_active = models.BooleanField(default=True)
    is_applied = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    
    # TenantMixin required field with default
    class Meta:
        app_label = 'bandwidth'
        ordering = ['-priority_level', 'name']
        verbose_name = "Traffic Rule"
        verbose_name_plural = "Traffic Rules"
    
    def __str__(self):
        return f"{self.name} ({self.rule_type})"


class DataUsage(models.Model):
    """Tracks customer data usage"""
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.CASCADE,
        related_name='data_usage'
    )
    bandwidth_profile = models.ForeignKey(BandwidthProfile, on_delete=models.SET_NULL, null=True)
    
    download_bytes = models.BigIntegerField(default=0)
    upload_bytes = models.BigIntegerField(default=0)
    total_bytes = models.BigIntegerField(default=0)
    
    peak_download_speed = models.FloatField(default=0)
    peak_upload_speed = models.FloatField(default=0)
    peak_time = models.DateTimeField(null=True, blank=True)
    
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    
    billing_cycle_id = models.PositiveIntegerField(null=True, blank=True)
    
    daily_usage = models.JSONField(default=dict)
    hourly_peak = models.JSONField(default=dict)
    
    is_over_limit = models.BooleanField(default=False)
    overage_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # TenantMixin required field with default
    class Meta:
        app_label = 'bandwidth'
        ordering = ['-period_end', 'customer']
        verbose_name = "Data Usage"
        verbose_name_plural = "Data Usage Records"
        unique_together = ['customer', 'period_start', 'period_end']
    
    def __str__(self):
        return f"{self.customer.customer_code} - {self.period_start.date()} to {self.period_end.date()}"
    
    @property
    def download_gb(self):
        return round(self.download_bytes / (1024**3), 2)
    
    @property
    def upload_gb(self):
        return round(self.upload_bytes / (1024**3), 2)
    
    @property
    def total_gb(self):
        return round(self.total_bytes / (1024**3), 2)
    
    @property
    def usage_percentage(self):
        if self.bandwidth_profile and self.bandwidth_profile.data_cap > 0:
            return min(100, (self.total_gb / self.bandwidth_profile.data_cap) * 100)
        return 0


class BandwidthAlert(models.Model):
    """Alerts for bandwidth usage thresholds"""
    ALERT_TYPES = [
        ('usage', 'Usage Threshold'),
        ('speed', 'Speed Anomaly'),
        ('security', 'Security Alert'),
        ('device', 'Device Offline'),
    ]
    
    ALERT_LEVELS = [
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('critical', 'Critical'),
    ]
    
    alert_type = models.CharField(max_length=20, choices=ALERT_TYPES)
    alert_level = models.CharField(max_length=20, choices=ALERT_LEVELS, default='warning')
    
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    device_id = models.PositiveIntegerField(null=True, blank=True)
    device_type = models.CharField(max_length=50, blank=True)
    
    threshold_percentage = models.PositiveIntegerField(validators=[MaxValueValidator(100)], default=80)
    threshold_value = models.FloatField(null=True, blank=True)
    threshold_unit = models.CharField(max_length=10, blank=True, choices=[('mbps', 'Mbps'), ('gb', 'GB'), ('%', 'Percentage')])
    
    message = models.TextField()
    triggered_value = models.FloatField(null=True, blank=True)
    
    notify_customer = models.BooleanField(default=False)
    notify_staff = models.BooleanField(default=True)
    notification_methods = models.JSONField(default=list)
    
    is_triggered = models.BooleanField(default=False)
    triggered_at = models.DateTimeField(null=True, blank=True)
    acknowledged = models.BooleanField(default=False)
    acknowledged_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)
    
    recurring = models.BooleanField(default=False)
    recurrence_interval = models.PositiveIntegerField(default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # TenantMixin required field with default
    class Meta:
        app_label = 'bandwidth'
        ordering = ['-triggered_at', 'alert_level']
        verbose_name = "Bandwidth Alert"
        verbose_name_plural = "Bandwidth Alerts"
    
    def __str__(self):
        return f"{self.alert_type} - {self.alert_level} - {self.customer.customer_code if self.customer else 'Device'}"
