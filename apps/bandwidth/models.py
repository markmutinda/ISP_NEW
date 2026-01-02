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
    
    # Bandwidth limits (in Mbps)
    download_speed = models.PositiveIntegerField(help_text="Download speed in Mbps")
    upload_speed = models.PositiveIntegerField(help_text="Upload speed in Mbps")
    
    # Data caps (in GB, 0 for unlimited)
    data_cap = models.PositiveIntegerField(default=0, help_text="Monthly data cap in GB (0 for unlimited)")
    burst_limit = models.PositiveIntegerField(default=0, help_text="Burst limit in Mbps (0 for no burst)")
    burst_duration = models.PositiveIntegerField(default=0, help_text="Burst duration in seconds")
    
    # QoS settings
    priority = models.IntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(10)])
    guaranteed_min_speed = models.PositiveIntegerField(default=0, help_text="Guaranteed minimum speed in Mbps")
    
    # Time-based restrictions
    peak_hours_only = models.BooleanField(default=False)
    peak_start_time = models.TimeField(null=True, blank=True)
    peak_end_time = models.TimeField(null=True, blank=True)
    
    # Pricing
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    
    # Company association
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='bandwidth_profiles',
        null=True,
        blank=True
    )
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
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
    
    # Source/Destination
    source_ip = models.GenericIPAddressField(null=True, blank=True, help_text="Source IP address")
    destination_ip = models.GenericIPAddressField(null=True, blank=True, help_text="Destination IP address")
    source_port = models.PositiveIntegerField(null=True, blank=True)
    destination_port = models.PositiveIntegerField(null=True, blank=True)
    protocol = models.CharField(max_length=10, choices=[('tcp', 'TCP'), ('udp', 'UDP'), ('icmp', 'ICMP'), ('any', 'Any')], default='any')
    
    # Bandwidth limits for this rule
    max_bandwidth = models.PositiveIntegerField(help_text="Maximum bandwidth in Kbps")
    guaranteed_bandwidth = models.PositiveIntegerField(default=0, help_text="Guaranteed bandwidth in Kbps")
    
    # Time restrictions
    schedule_enabled = models.BooleanField(default=False)
    schedule_start = models.TimeField(null=True, blank=True)
    schedule_end = models.TimeField(null=True, blank=True)
    schedule_days = models.CharField(max_length=50, default='mon,tue,wed,thu,fri,sat,sun')
    
    # Application/Content filtering
    application_protocol = models.CharField(max_length=50, blank=True, help_text="e.g., HTTP, FTP, DNS, VoIP")
    content_filter = models.TextField(blank=True, help_text="Regex pattern for content filtering")
    
    # Device targeting (using string references to avoid circular imports)
    target_device_id = models.PositiveIntegerField(null=True, blank=True, help_text="ID of the target device")
    target_device_type = models.CharField(max_length=50, blank=True, help_text="Type of target device: mikrotik, olt, cpe")
    
    # Status
    is_active = models.BooleanField(default=True)
    is_applied = models.BooleanField(default=False, help_text="Whether the rule has been applied to devices")
    
    # Company association
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='traffic_rules',
        null=True,
        blank=True
    )
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
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
    
    # Current usage
    download_bytes = models.BigIntegerField(default=0, help_text="Download in bytes")
    upload_bytes = models.BigIntegerField(default=0, help_text="Upload in bytes")
    total_bytes = models.BigIntegerField(default=0, help_text="Total bytes transferred")
    
    # Peak usage
    peak_download_speed = models.FloatField(default=0, help_text="Peak download speed in Mbps")
    peak_upload_speed = models.FloatField(default=0, help_text="Peak upload speed in Mbps")
    peak_time = models.DateTimeField(null=True, blank=True)
    
    # Time period
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    
    # Billing cycle info (using string reference)
    billing_cycle_id = models.PositiveIntegerField(null=True, blank=True)
    
    # Cache for quick access
    daily_usage = models.JSONField(default=dict, help_text="Daily usage breakdown")
    hourly_peak = models.JSONField(default=dict, help_text="Hourly peak usage data")
    
    # Status
    is_over_limit = models.BooleanField(default=False)
    overage_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
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
    
    # Target
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    device_id = models.PositiveIntegerField(null=True, blank=True)
    device_type = models.CharField(max_length=50, blank=True)
    
    # Thresholds
    threshold_percentage = models.PositiveIntegerField(validators=[MaxValueValidator(100)], default=80)
    threshold_value = models.FloatField(null=True, blank=True, help_text="Absolute threshold value")
    threshold_unit = models.CharField(max_length=10, blank=True, choices=[('mbps', 'Mbps'), ('gb', 'GB'), ('%', 'Percentage')])
    
    # Alert details
    message = models.TextField()
    triggered_value = models.FloatField(null=True, blank=True)
    
    # Notification settings
    notify_customer = models.BooleanField(default=False)
    notify_staff = models.BooleanField(default=True)
    notification_methods = models.JSONField(default=list, help_text="List of notification methods: email, sms, push")
    
    # Status
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
    
    # Resolution
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)
    
    # Recurring alerts
    recurring = models.BooleanField(default=False)
    recurrence_interval = models.PositiveIntegerField(default=0, help_text="Recurrence interval in minutes (0 for one-time)")
    
    # Company association
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='bandwidth_alerts',
        null=True,
        blank=True
    )
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-triggered_at', 'alert_level']
        verbose_name = "Bandwidth Alert"
        verbose_name_plural = "Bandwidth Alerts"
    
    def __str__(self):
        return f"{self.alert_type} - {self.alert_level} - {self.customer.customer_code if self.customer else 'Device'}"


class SpeedTestResult(models.Model):
    """Speed test results from customers"""
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.CASCADE
    )
    device_id = models.PositiveIntegerField(null=True, blank=True)
    device_type = models.CharField(max_length=50, blank=True)
    
    # Test results
    download_speed = models.FloatField(help_text="Download speed in Mbps")
    upload_speed = models.FloatField(help_text="Upload speed in Mbps")
    latency = models.FloatField(help_text="Latency in ms")
    jitter = models.FloatField(help_text="Jitter in ms")
    packet_loss = models.FloatField(help_text="Packet loss percentage")
    
    # Test server
    server_name = models.CharField(max_length=200, blank=True)
    server_location = models.CharField(max_length=200, blank=True)
    
    # Test metadata
    test_method = models.CharField(max_length=50, choices=[('ookla', 'Ookla'), ('speedtest', 'Speedtest.net'), ('custom', 'Custom'), ('router', 'Router-based')])
    test_duration = models.PositiveIntegerField(help_text="Test duration in seconds")
    
    # Network conditions
    concurrent_users = models.PositiveIntegerField(null=True, blank=True)
    network_load = models.FloatField(null=True, blank=True, help_text="Network load percentage")
    
    # Result validation
    is_valid = models.BooleanField(default=True)
    validation_notes = models.TextField(blank=True)
    
    # Company association
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='speed_test_results',
        null=True,
        blank=True
    )
    
    # Metadata
    test_time = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-test_time']
        verbose_name = "Speed Test Result"
        verbose_name_plural = "Speed Test Results"
    
    def __str__(self):
        return f"{self.customer.customer_code} - {self.download_speed}/{self.upload_speed} Mbps"