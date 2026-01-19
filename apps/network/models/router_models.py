# apps/network/models/router_models.py
from operator import mod
import secrets
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone

from apps.core.models import Company, AuditMixin, Tenant  # ADD Tenant import
from apps.customers.models import ServiceConnection


def generate_auth_key():
    random_part = secrets.token_hex(4).upper()
    return f"RTR_{random_part}_AUTH"


def generate_shared_secret():
    """Generate a strong random shared secret for RADIUS communication"""
    return secrets.token_hex(16)


class Router(AuditMixin):
    ROUTER_TYPES = [
        ('mikrotik', 'Mikrotik'),
        ('ubiquiti', 'Ubiquiti'),
        ('cisco', 'Cisco'),
        ('other', 'Other'),
    ]

    STATUS_CHOICES = [
        ('online', 'Online'),
        ('offline', 'Offline'),
        ('maintenance', 'Maintenance'),
        ('warning', 'Warning'),
        ('error', 'Error'),
    ]

    # ADD THIS COMPANY RELATIONSHIP
    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name='routers',
        null=True,  # Make nullable temporarily for existing data
        blank=True,
        help_text="Company this router belongs to"
    )

    name = models.CharField(max_length=255, help_text="Friendly name for the router")
    ip_address = models.GenericIPAddressField(
        protocol='both',
        null=True,
        blank=True,
        help_text="Management IP — auto-filled on authentication"
    )
    mac_address = models.CharField(
        max_length=17,
        null=True,
        blank=True,
        help_text="WAN MAC address (optional)"
    )
    api_port = models.PositiveIntegerField(
        default=8728,
        validators=[MinValueValidator(1), MaxValueValidator(65535)]
    )
    api_username = models.CharField(max_length=100, null=True, blank=True)
    api_password = models.CharField(max_length=255, null=True, blank=True)
    router_type = models.CharField(max_length=50, choices=ROUTER_TYPES, default='mikrotik')
    model = models.CharField(max_length=100, null=True, blank=True)
    firmware_version = models.CharField(max_length=50, null=True, blank=True)
    location = models.CharField(max_length=255, null=True, blank=True)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='offline')
    total_users = models.PositiveIntegerField(default=0)
    active_users = models.PositiveIntegerField(default=0)
    uptime = models.CharField(max_length=50, null=True, blank=True)
    uptime_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    sla_target = models.DecimalField(max_digits=5, decimal_places=2, default=99.00)
    last_seen = models.DateTimeField(null=True, blank=True)
    tags = models.JSONField(default=list)
    notes = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # Authentication fields for router self-registration
    auth_key = models.CharField(
        max_length=50,
        unique=True,
        default=generate_auth_key,
        help_text="Auto-generated key used by router script to authenticate"
    )
    is_authenticated = models.BooleanField(default=False)
    authenticated_at = models.DateTimeField(null=True, blank=True)

    # Shared secret for RADIUS communication with this router
    shared_secret = models.CharField(
        max_length=255,
        default=generate_shared_secret,
        help_text="RADIUS shared secret — used when configuring this router as a RADIUS client"
    )
    
    # Tenant schema field - REMOVE OR MAKE NON-UNIQUE
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        null=True,
        blank=True,
        help_text="Tenant schema name (auto-filled from company)"
    )

    class Meta:
        verbose_name = 'Router'
        verbose_name_plural = 'Routers'
        ordering = ['-created_at']
        # REMOVE unique_together for name or make it unique per company
        # unique_together = ['name']
        indexes = [
            models.Index(fields=['ip_address']),
            models.Index(fields=['status']),
            models.Index(fields=['last_seen']),
            models.Index(fields=['auth_key']),
            models.Index(fields=['company']),  # ADD this index
        ]

    def __str__(self):
        ip = f" ({self.ip_address})" if self.ip_address else ""
        company = f" - {self.company.name}" if self.company else ""
        return f"{self.name}{ip}{company}"
    
    def save(self, *args, **kwargs):
        # Auto-fill schema_name from company's tenant if available
        if self.company and hasattr(self.company, 'tenant') and self.company.tenant:
            self.schema_name = self.company.tenant.schema_name
        super().save(*args, **kwargs)


class RouterEvent(AuditMixin):
    EVENT_TYPES = [
        ('up', 'Router Online'),
        ('down', 'Router Offline'),
        ('reboot', 'Router Rebooted'),
        ('config_change', 'Configuration Changed'),
        ('warning', 'Warning'),
        ('error', 'Error'),
        ('maintenance', 'Maintenance Mode'),
        ('auth_success', 'Authenticated Successfully'),
        ('auth_key_regen', 'Auth Key Regenerated'),
        ('backup', 'Backup Created'),
    ]

    router = models.ForeignKey(Router, on_delete=models.CASCADE, related_name='events')
    event_type = models.CharField(max_length=50, choices=EVENT_TYPES)
    message = models.TextField()
    
    # Make schema_name non-unique
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        null=True,
        blank=True
    )
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
    
    def save(self, *args, **kwargs):
        # Auto-fill schema_name from router
        if self.router and self.router.schema_name:
            self.schema_name = self.router.schema_name
        super().save(*args, **kwargs)


# ====================== SUB-MODELS ======================

class MikrotikInterface(AuditMixin):
    router = models.ForeignKey(Router, on_delete=models.CASCADE, related_name='interfaces')
    interface_name = models.CharField(max_length=50)
    interface_type = models.CharField(max_length=20, choices=[
        ('ETHERNET', 'Ethernet'),
        ('WLAN', 'Wireless'),
        ('BRIDGE', 'Bridge'),
        ('VLAN', 'VLAN'),
        ('PPPOE', 'PPPoE'),
        ('OTHER', 'Other'),
    ], default='ETHERNET')
    mac_address = models.CharField(max_length=17, blank=True)
    mtu = models.IntegerField(default=1500)
    rx_bytes = models.BigIntegerField(default=0)
    tx_bytes = models.BigIntegerField(default=0)
    rx_packets = models.BigIntegerField(default=0)
    tx_packets = models.BigIntegerField(default=0)
    rx_errors = models.BigIntegerField(default=0)
    tx_errors = models.BigIntegerField(default=0)
    admin_state = models.BooleanField(default=True)
    operational_state = models.BooleanField(default=False)
    last_change = models.DateTimeField(auto_now=True)
    
    # Make schema_name non-unique
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        null=True,
        blank=True
    )

    class Meta:
        unique_together = [['router', 'interface_name']]
        ordering = ['interface_name']

    def __str__(self):
        return f"{self.router.name} - {self.interface_name}"
    
    def save(self, *args, **kwargs):
        if self.router and self.router.schema_name:
            self.schema_name = self.router.schema_name
        super().save(*args, **kwargs)


class HotspotUser(AuditMixin):
    router = models.ForeignKey(Router, on_delete=models.CASCADE, related_name='hotspot_users')
    service_connection = models.OneToOneField(
        ServiceConnection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hotspot_user'
    )
    username = models.CharField(max_length=100)
    password = models.CharField(max_length=100)
    mac_address = models.CharField(max_length=17, blank=True)
    ip_address = models.GenericIPAddressField(protocol='IPv4', null=True, blank=True)
    bytes_in = models.BigIntegerField(default=0)
    bytes_out = models.BigIntegerField(default=0)
    status = models.CharField(max_length=20, choices=[
        ('ACTIVE', 'Active'),
        ('DISABLED', 'Disabled'),
        ('EXPIRED', 'Expired'),
        ('BLOCKED', 'Blocked'),
    ], default='ACTIVE')
    profile = models.CharField(max_length=100, default='default')
    
    # Make schema_name non-unique
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        null=True,
        blank=True
    )

    class Meta:
        unique_together = [['router', 'username']]
        ordering = ['username']

    def __str__(self):
        return f"{self.username}@{self.router.name}"
    
    def save(self, *args, **kwargs):
        if self.router and self.router.schema_name:
            self.schema_name = self.router.schema_name
        super().save(*args, **kwargs)


class PPPoEUser(AuditMixin):
    router = models.ForeignKey(Router, on_delete=models.CASCADE, related_name='pppoe_users')
    service_connection = models.OneToOneField(
        ServiceConnection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pppoe_user'
    )
    username = models.CharField(max_length=100)
    password = models.CharField(max_length=100)
    caller_id = models.CharField(max_length=100, blank=True)
    local_address = models.GenericIPAddressField(protocol='IPv4', null=True, blank=True)
    remote_address = models.GenericIPAddressField(protocol='IPv4', null=True, blank=True)
    bytes_in = models.BigIntegerField(default=0)
    bytes_out = models.BigIntegerField(default=0)
    status = models.CharField(max_length=20, choices=[
        ('CONNECTED', 'Connected'),
        ('DISCONNECTED', 'Disconnected'),
        ('DISABLED', 'Disabled'),
    ], default='DISCONNECTED')
    profile = models.CharField(max_length=100, default='default-encryption')
    
    # Make schema_name non-unique
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        null=True,
        blank=True
    )

    class Meta:
        unique_together = [['router', 'username']]
        ordering = ['username']

    def __str__(self):
        return f"{self.username}@{self.router.name}"
    
    def save(self, *args, **kwargs):
        if self.router and self.router.schema_name:
            self.schema_name = self.router.schema_name
        super().save(*args, **kwargs)


class MikrotikQueue(AuditMixin):
    router = models.ForeignKey(Router, on_delete=models.CASCADE, related_name='queues')
    queue_name = models.CharField(max_length=100)
    queue_type = models.CharField(max_length=20, default='SIMPLE')
    target = models.CharField(max_length=200)
    max_limit = models.CharField(max_length=50, default='10M/10M')
    burst_limit = models.CharField(max_length=50, blank=True)
    disabled = models.BooleanField(default=False)
    comment = models.TextField(blank=True)

    # Optional links to users
    hotspot_user = models.ForeignKey(HotspotUser, on_delete=models.SET_NULL, null=True, blank=True)
    pppoe_user = models.ForeignKey(PPPoEUser, on_delete=models.SET_NULL, null=True, blank=True)
    
    # Make schema_name non-unique
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        null=True,
        blank=True
    )

    class Meta:
        unique_together = [['router', 'queue_name']]
        ordering = ['queue_name']

    def __str__(self):
        return f"{self.queue_name} - {self.router.name}"
    
    def save(self, *args, **kwargs):
        if self.router and self.router.schema_name:
            self.schema_name = self.router.schema_name
        super().save(*args, **kwargs)