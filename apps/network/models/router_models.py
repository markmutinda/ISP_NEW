# apps/network/models/router_models.py

from operator import mod
import secrets
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone

from apps.core.models import AuditMixin, Tenant  # ADD Tenant import
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
    
    # Configuration Types
    CONFIG_TYPES = [
        ('basic', 'Basic Router'),
        ('hotspot', 'Hotspot Only'),
        ('pppoe', 'PPPoE Only'),
        ('isp', 'Full ISP (Hotspot + PPPoE)'),
        ('full_isp', 'Full ISP with OpenVPN'),
    ]

    # DENORMALIZED FIELDS (replace ForeignKey to Company)
    company_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Company name (denormalized from public schema)"
    )
    
    tenant_subdomain = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Tenant subdomain (denormalized from public schema)"
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
    
    # Configuration Type
    config_type = models.CharField(
        max_length=20,
        choices=CONFIG_TYPES,
        default='basic'
    )
    
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
    
    # Network configuration fields
    lan_subnet = models.CharField(
        max_length=20, 
        default='192.168.88.0/24',
        help_text="LAN network subnet (e.g., 192.168.88.0/24)"
    )
    hotspot_subnet = models.CharField(
        max_length=20, 
        default='172.19.0.0/16',
        help_text="Hotspot network subnet (e.g., 172.19.0.0/16)"
    )
    pppoe_pool = models.CharField(
        max_length=50, 
        default='192.40.2.10-192.40.2.254',
        help_text="PPPoE IP pool range"
    )
    
    # Service enable/disable flags
    enable_hotspot = models.BooleanField(
        default=True,
        help_text="Enable Hotspot service"
    )
    enable_pppoe = models.BooleanField(
        default=True,
        help_text="Enable PPPoE service"
    )
    enable_openvpn = models.BooleanField(
        default=False,
        help_text="Enable OpenVPN client (for backhaul)"
    )
    
    # OpenVPN settings
    openvpn_server = models.CharField(
        max_length=100, 
        default='vpn.yourisp.local',
        help_text="OpenVPN server address"
    )
    openvpn_port = models.IntegerField(
        default=1194,
        help_text="OpenVPN server port"
    )
    openvpn_username = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="OpenVPN username (auto-generated if empty)"
    )
    openvpn_password = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="OpenVPN password (auto-generated if empty)"
    )
    
    # Hotspot settings
    hotspot_portal_url = models.URLField(
        default='https://app.yourisp.local',
        help_text="Hotspot captive portal URL"
    )
    hotspot_cookie_lifetime = models.CharField(
        max_length=10, 
        default='4w2d',
        help_text="Hotspot cookie lifetime (e.g., 4w2d)"
    )
    hotspot_ssl_cert = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="SSL certificate name for Hotspot HTTPS"
    )
    
    # WAN Interface configuration
    wan_interface = models.CharField(
        max_length=50,
        default='ether1',
        help_text="WAN interface name (e.g., ether1, sfp-sfpplus1)"
    )
    
    # LAN Interface configuration
    lan_interfaces = models.CharField(
        max_length=200,
        default='ether2,ether3,ether4,ether5',
        help_text="Comma-separated LAN interfaces"
    )
    
    # RADIUS Settings
    radius_server = models.GenericIPAddressField(
        protocol='IPv4',
        blank=True,
        null=True,
        help_text="RADIUS server IP address"
    )
    radius_port = models.IntegerField(
        default=1812,
        help_text="RADIUS authentication port"
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
        indexes = [
            models.Index(fields=['ip_address']),
            models.Index(fields=['status']),
            models.Index(fields=['last_seen']),
            models.Index(fields=['auth_key']),
            models.Index(fields=['company_name']),
            models.Index(fields=['tenant_subdomain']),
            models.Index(fields=['config_type']),
        ]

    def __str__(self):
        ip = f" ({self.ip_address})" if self.ip_address else ""
        company = f" - {self.company_name}" if self.company_name else ""
        return f"{self.name}{ip}{company}"
    
    def save(self, *args, **kwargs):
        # Auto-fill schema_name from tenant_subdomain if available
        if self.tenant_subdomain:
            self.schema_name = f"tenant_{self.tenant_subdomain}"
        
        # Generate OpenVPN credentials if enabled and not set
        if self.enable_openvpn and not self.openvpn_username:
            import uuid
            self.openvpn_username = f"{self.name.lower().replace(' ', '_')}_{self.id}_vpn"
            self.openvpn_password = secrets.token_hex(8)
        
        # Ensure shared secret exists
        if not self.shared_secret or self.shared_secret == '':
            self.shared_secret = generate_shared_secret()
        
        super().save(*args, **kwargs)
    
    def get_lan_ip(self):
        """Extract LAN gateway IP from subnet"""
        if '/' in self.lan_subnet:
            network, cidr = self.lan_subnet.split('/')
            parts = network.split('.')
            parts[-1] = '1'
            return '.'.join(parts)
        return '192.168.88.1'
    
    def get_hotspot_ip(self):
        """Extract Hotspot gateway IP from subnet"""
        if '/' in self.hotspot_subnet:
            network, cidr = self.hotspot_subnet.split('/')
            parts = network.split('.')
            parts[-1] = '1'
            return '.'.join(parts)
        return '172.19.0.1'
    
    def get_pppoe_local_ip(self):
        """Get PPPoE local address"""
        if '-' in self.pppoe_pool:
            return self.pppoe_pool.split('-')[0].rsplit('.', 1)[0] + '.1'
        return '192.40.2.1'


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
        ('user_created', 'User Created'),
        ('user_deleted', 'User Deleted'),
        ('user_enabled', 'User Enabled'),
        ('user_disabled', 'User Disabled'),
        ('queue_created', 'Queue Created'),
        ('queue_removed', 'Queue Removed'),
        ('interface_up', 'Interface Up'),
        ('interface_down', 'Interface Down'),
        ('config_sync', 'Configuration Synced'),
        ('script_executed', 'Script Executed'),
    ]

    router = models.ForeignKey(Router, on_delete=models.CASCADE, related_name='events')
    event_type = models.CharField(max_length=50, choices=EVENT_TYPES)
    message = models.TextField()
    
    # Additional details for events
    details = models.JSONField(default=dict, blank=True, null=True)
    
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
        indexes = [
            models.Index(fields=['router', 'event_type']),
            models.Index(fields=['created_at']),
        ]
    
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
    
    # Connection tracking
    connected_since = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    
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
        indexes = [
            models.Index(fields=['router', 'status']),
            models.Index(fields=['last_seen']),
        ]

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
    
    # Connection tracking
    connected_since = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    
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
        indexes = [
            models.Index(fields=['router', 'status']),
            models.Index(fields=['last_seen']),
        ]

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


class RouterConfiguration(AuditMixin):
    """Store router configuration templates and history"""
    router = models.ForeignKey(Router, on_delete=models.CASCADE, related_name='configurations')
    config_type = models.CharField(max_length=20, choices=Router.CONFIG_TYPES)
    config_data = models.JSONField(default=dict, help_text="Configuration parameters")
    config_script = models.TextField(help_text="Generated RouterOS script")
    version = models.CharField(max_length=10, default='1.0')
    is_active = models.BooleanField(default=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    applied_by = models.CharField(max_length=100, blank=True, null=True)
    
    # Make schema_name non-unique
    schema_name = models.SlugField(
        max_length=63,
        editable=False,
        null=True,
        blank=True
    )
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['router', 'is_active']),
            models.Index(fields=['applied_at']),
        ]
    
    def __str__(self):
        return f"{self.router.name} - {self.config_type} v{self.version}"
    
    def save(self, *args, **kwargs):
        if self.router and self.router.schema_name:
            self.schema_name = self.router.schema_name
        super().save(*args, **kwargs)