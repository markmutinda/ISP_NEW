"""
Hotspot Models for WiFi Access Payments

These models handle hotspot/captive portal functionality where
end-users pay for WiFi access at hotspot locations.

These models live in TENANT schema as they're per-ISP.
"""

import secrets
import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import models
from django.utils import timezone


class HotspotPlan(models.Model):
    """
    Hotspot access plans configured per router.
    End users select these plans when connecting to WiFi.
    """
    
    SPEED_CHOICES = (
        ('1', '1 Mbps'),
        ('2', '2 Mbps'),
        ('5', '5 Mbps'),
        ('10', '10 Mbps'),
        ('15', '15 Mbps'),
        ('20', '20 Mbps'),
        ('50', '50 Mbps'),
        ('100', '100 Mbps'),
    )
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Relationship to router
    router = models.ForeignKey(
        'network.Router',
        on_delete=models.CASCADE,
        related_name='hotspot_plans'
    )
    
    # Plan Details
    name = models.CharField(max_length=100, help_text="e.g., '1 Hour', 'Daily Pass'")
    description = models.TextField(blank=True)
    
    # Pricing
    price = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='KES')
    
    # Duration
    duration_minutes = models.PositiveIntegerField(
        help_text="Access duration in minutes (e.g., 60 for 1 hour)"
    )
    
    # Data & Speed Limits
    data_limit_mb = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Data limit in MB. Null = unlimited"
    )
    speed_limit_mbps = models.CharField(
        max_length=10,
        choices=SPEED_CHOICES,
        default='5',
        help_text="Speed limit in Mbps"
    )
    
    # MikroTik Integration
    mikrotik_profile = models.CharField(
        max_length=100,
        blank=True,
        help_text="MikroTik hotspot user profile name"
    )
    
    # Display
    is_active = models.BooleanField(default=True)
    is_popular = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    
    # Metadata
    created_by = models.ForeignKey(
        'core.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_hotspot_plans'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['router', 'sort_order', 'price']
        verbose_name = 'Hotspot Plan'
        verbose_name_plural = 'Hotspot Plans'
        unique_together = ['router', 'name']
    
    def __str__(self):
        return f"{self.router.name} - {self.name} (KES {self.price})"
    
    @property
    def duration_display(self) -> str:
        """Human-readable duration"""
        minutes = self.duration_minutes
        if minutes < 60:
            return f"{minutes} minutes"
        elif minutes < 1440:
            hours = minutes // 60
            return f"{hours} hour{'s' if hours > 1 else ''}"
        else:
            days = minutes // 1440
            return f"{days} day{'s' if days > 1 else ''}"
    
    @property
    def data_limit_display(self) -> str:
        """Human-readable data limit"""
        if not self.data_limit_mb:
            return "Unlimited"
        elif self.data_limit_mb >= 1024:
            gb = self.data_limit_mb / 1024
            return f"{gb:.1f} GB"
        else:
            return f"{self.data_limit_mb} MB"


class HotspotSession(models.Model):
    """
    Tracks a hotspot purchase/session from payment to activation.
    Created when user initiates payment, updated on payment completion.
    """
    
    STATUS_CHOICES = (
        ('pending', 'Pending Payment'),
        ('paid', 'Paid - Activating'),
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('failed', 'Payment Failed'),
        ('cancelled', 'Cancelled'),
    )
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Unique session identifier (shown to user)
    session_id = models.CharField(
        max_length=50,
        unique=True,
        help_text="Unique session ID like HS_1234567890_ABCD"
    )
    
    # Relationships
    router = models.ForeignKey(
        'network.Router',
        on_delete=models.CASCADE,
        related_name='hotspot_sessions'
    )
    plan = models.ForeignKey(
        HotspotPlan,
        on_delete=models.CASCADE,
        related_name='sessions'
    )
    
    # User Details (no auth required for hotspot)
    phone_number = models.CharField(max_length=15)
    mac_address = models.CharField(
        max_length=17,
        help_text="Device MAC address (AA:BB:CC:DD:EE:FF)"
    )
    
    # Payment Details
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payhero_checkout_id = models.CharField(max_length=100, blank=True, null=True)
    mpesa_receipt = models.CharField(max_length=50, blank=True, null=True)
    
    # Session Details
    access_code = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="WiFi access code (e.g., WIFI-1234)"
    )
    
    # Status & Timing
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    activated_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    
    # Data Usage (updated by MikroTik)
    data_used_mb = models.PositiveIntegerField(default=0)
    
    # Failure tracking
    failure_reason = models.TextField(blank=True, null=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Hotspot Session'
        verbose_name_plural = 'Hotspot Sessions'
        indexes = [
            models.Index(fields=['session_id']),
            models.Index(fields=['payhero_checkout_id']),
            models.Index(fields=['phone_number']),
            models.Index(fields=['mac_address']),
            models.Index(fields=['status']),
        ]
    
    def __str__(self):
        return f"{self.session_id} - {self.phone_number} ({self.status})"
    
    @classmethod
    def generate_session_id(cls) -> str:
        """Generate unique session ID"""
        timestamp = int(timezone.now().timestamp())
        random_part = secrets.token_hex(2).upper()
        return f"HS_{timestamp}_{random_part}"
    
    @classmethod
    def generate_access_code(cls) -> str:
        """Generate WiFi access code"""
        return f"WIFI-{secrets.token_hex(2).upper()}"
    
    def activate(self, access_code: str = None):
        """
        Mark session as active after successful payment.
        Sets access code and expiration time.
        """
        self.status = 'active'
        self.access_code = access_code or self.generate_access_code()
        self.activated_at = timezone.now()
        self.expires_at = timezone.now() + timedelta(minutes=self.plan.duration_minutes)
        self.save()
    
    def mark_paid(self, mpesa_receipt: str = None):
        """Mark as paid, pending activation"""
        self.status = 'paid'
        if mpesa_receipt:
            self.mpesa_receipt = mpesa_receipt
        self.save()
    
    def mark_failed(self, reason: str = None):
        """Mark payment as failed"""
        self.status = 'failed'
        self.failure_reason = reason
        self.save()
    
    def mark_expired(self):
        """Mark session as expired"""
        self.status = 'expired'
        self.save()
    
    @property
    def is_active(self) -> bool:
        """Check if session is currently active"""
        if self.status != 'active':
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        return True
    
    @property
    def time_remaining_minutes(self) -> int:
        """Minutes remaining in session"""
        if not self.is_active or not self.expires_at:
            return 0
        delta = self.expires_at - timezone.now()
        return max(0, int(delta.total_seconds() / 60))
    
    @property
    def data_remaining_mb(self) -> int:
        """Data remaining in MB (None if unlimited)"""
        if not self.plan.data_limit_mb:
            return None
        return max(0, self.plan.data_limit_mb - self.data_used_mb)


class HotspotBranding(models.Model):
    """
    Branding configuration for captive portal.
    Customizes the look and feel of the WiFi login page.
    """
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Can be per-router or per-tenant
    router = models.OneToOneField(
        'network.Router',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='hotspot_branding'
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Default branding for all routers without specific branding"
    )
    
    # Visual Branding
    company_name = models.CharField(max_length=100)
    logo = models.ImageField(
        upload_to='hotspot/logos/',
        null=True,
        blank=True
    )
    background_image = models.ImageField(
        upload_to='hotspot/backgrounds/',
        null=True,
        blank=True
    )
    
    # Colors
    primary_color = models.CharField(max_length=7, default='#3B82F6')
    secondary_color = models.CharField(max_length=7, default='#1E40AF')
    text_color = models.CharField(max_length=7, default='#1F2937')
    background_color = models.CharField(max_length=7, default='#FFFFFF')
    
    # Content
    welcome_title = models.CharField(max_length=200, default='Welcome to WiFi')
    welcome_message = models.TextField(blank=True)
    terms_and_conditions = models.TextField(blank=True)
    support_phone = models.CharField(max_length=20, blank=True)
    support_email = models.EmailField(blank=True)
    
    # Social Links
    facebook_url = models.URLField(blank=True)
    twitter_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)
    website_url = models.URLField(blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'Hotspot Branding'
        verbose_name_plural = 'Hotspot Branding'
    
    def __str__(self):
        if self.router:
            return f"Branding for {self.router.name}"
        return f"Default Branding - {self.company_name}"
