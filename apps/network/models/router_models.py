# apps/network/models/router_models.py

import secrets
import uuid
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from apps.core.models import AuditMixin, Tenant

# NOTE: Do NOT import ServiceConnection here to avoid Circular Import errors.
# We will use 'customers.ServiceConnection' as a string reference instead.

def generate_auth_key():
    random_part = secrets.token_hex(4).upper()
    return f"RTR_{random_part}_AUTH"

def generate_shared_secret():
    return secrets.token_hex(16)

class Router(AuditMixin):
    # ────────────────────────────────────────────────────────────────
    # CONSTANTS & CHOICES
    # ────────────────────────────────────────────────────────────────
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
    
    CONFIG_TYPES = [
        ('basic', 'Basic Router'),
        ('hotspot', 'Hotspot Only'),
        ('pppoe', 'PPPoE Only'),
        ('isp', 'Full ISP (Hotspot + PPPoE)'),
        ('full_isp', 'Full ISP with OpenVPN'),
    ]

    # ────────────────────────────────────────────────────────────────
    # IDENTITY & TENANCY
    # ────────────────────────────────────────────────────────────────
    name = models.CharField(max_length=255, help_text="Friendly name (e.g. 'Site A Router')")
    
    # Denormalized fields for quick access
    company_name = models.CharField(max_length=255, blank=True, null=True)
    tenant_subdomain = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    
    # Schema name helper for django-tenants context switching
    schema_name = models.SlugField(max_length=63, editable=False, null=True, blank=True)

    # ────────────────────────────────────────────────────────────────
    # AUTHENTICATION (Zero-Touch Core)
    # ────────────────────────────────────────────────────────────────
    auth_key = models.CharField(
        max_length=50,
        unique=True,
        default=generate_auth_key,
        help_text="The key used in the 'One-Liner' script."
    )
    is_authenticated = models.BooleanField(default=False)
    authenticated_at = models.DateTimeField(null=True, blank=True)

    # API Credentials (The script creates these ON the router)
    api_username = models.CharField(max_length=100, default='netily_api')
    api_password = models.CharField(max_length=255, blank=True, help_text="Auto-generated on save")
    api_port = models.PositiveIntegerField(default=8728)

    # RADIUS Security
    shared_secret = models.CharField(
        max_length=255,
        default=generate_shared_secret,
        help_text="Secret shared between Router and RADIUS server"
    )

    # ────────────────────────────────────────────────────────────────
    # NETWORK CONFIGURATION (The "Info" Tab Logic)
    # ────────────────────────────────────────────────────────────────
    # This single field drives the entire IP logic (Gateway, Pool, DHCP)
    gateway_cidr = models.CharField(
        max_length=20, 
        default='172.18.0.1/16',
        help_text="The main Gateway IP/Subnet (e.g., 172.18.0.1/16). Pool is calculated from this."
    )
    
    dns_name = models.CharField(
        max_length=100, 
        default='captive.netily.io',
        help_text="DNS name for the hotspot (e.g., login.wifi)"
    )

    # Interface Assignments (The "Check Ports" Feature)
    # Stored as a JSON list: ["ether2", "ether3", "wlan1"]
    hotspot_interfaces = models.JSONField(
        default=list, 
        blank=True,
        help_text="List of interfaces assigned to the Hotspot Bridge"
    )
    
    wan_interface = models.CharField(max_length=50, default='ether1')

    # ────────────────────────────────────────────────────────────────
    # VPN & MANAGEMENT TUNNEL
    # ────────────────────────────────────────────────────────────────
    enable_openvpn = models.BooleanField(default=True)
    openvpn_server = models.CharField(max_length=100, default='vpn.yourisp.com')
    openvpn_port = models.IntegerField(default=1194)
    
    # VPN Creds (Auto-generated per tenant)
    openvpn_username = models.CharField(max_length=100, blank=True, null=True)
    openvpn_password = models.CharField(max_length=100, blank=True, null=True)
    
    # The actual IP the router gets inside the VPN (e.g., 10.8.0.5)
    ip_address = models.GenericIPAddressField(
        protocol='IPv4', 
        null=True, 
        blank=True,
        help_text="Management IP (VPN Address)"
    )

    # ────────────────────────────────────────────────────────────────
    # CERTIFICATE-BASED VPN (Cloud Controller)
    # ────────────────────────────────────────────────────────────────
    # PEM certificate content stored for injection into .rsc scripts
    ca_certificate = models.TextField(
        blank=True, null=True,
        help_text="PEM content of ca.crt for this router's VPN"
    )
    client_certificate = models.TextField(
        blank=True, null=True,
        help_text="PEM content of client.crt"
    )
    client_key = models.TextField(
        blank=True, null=True,
        help_text="PEM content of client.key (should be encrypted at rest)"
    )
    # Static VPN IP mapped via CCD (Client Config Directory)
    vpn_ip_address = models.GenericIPAddressField(
        protocol='IPv4',
        null=True,
        blank=True,
        unique=True,
        help_text="Static IP assigned in OpenVPN CCD (e.g. 10.8.0.55)"
    )
    # FK to the VPN certificate record for lifecycle management
    vpn_certificate = models.ForeignKey(
        'vpn.VPNCertificate',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='provisioned_routers',
        help_text="The active VPN certificate for this router"
    )
    vpn_provisioned = models.BooleanField(
        default=False,
        help_text="Whether VPN certificates and CCD have been provisioned"
    )
    vpn_provisioned_at = models.DateTimeField(null=True, blank=True)
    vpn_last_seen = models.DateTimeField(
        null=True, blank=True,
        help_text="Last time this router was seen connected via VPN tunnel"
    )

    # ────────────────────────────────────────────────────────────────
    # SERVICE FLAGS & LEGACY COMPATIBILITY
    # ────────────────────────────────────────────────────────────────
    router_type = models.CharField(max_length=50, choices=ROUTER_TYPES, default='mikrotik')
    config_type = models.CharField(max_length=20, choices=CONFIG_TYPES, default='isp')
    
    enable_hotspot = models.BooleanField(default=True)
    enable_pppoe = models.BooleanField(default=True)
    
    pppoe_pool = models.CharField(max_length=50, default='192.40.2.10-192.40.2.254')
    pppoe_local_address = models.GenericIPAddressField(
        protocol='IPv4', null=True, blank=True, default='192.40.2.1',
        help_text="PPPoE server local address (service-name gateway)"
    )

    # ────────────────────────────────────────────────────────────────
    # HOTSPOT SSL CERTIFICATES (for HTTPS captive portal redirect)
    # ────────────────────────────────────────────────────────────────
    ssl_certificate = models.TextField(
        blank=True, null=True,
        help_text="PEM content of SSL cert for hotspot HTTPS (e.g. *.yourisp.com)"
    )
    ssl_private_key = models.TextField(
        blank=True, null=True,
        help_text="PEM content of SSL private key"
    )
    ssl_passphrase = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Passphrase for the SSL key (if encrypted)"
    )

    # ────────────────────────────────────────────────────────────────
    # PROVISIONING STATE
    # ────────────────────────────────────────────────────────────────
    provision_slug = models.SlugField(
        max_length=20, unique=True, blank=True, null=True,
        help_text="Short URL-safe slug for magic-link downloads (auto-generated)"
    )
    last_provisioned_at = models.DateTimeField(null=True, blank=True)
    routeros_version = models.CharField(
        max_length=10, blank=True, null=True,
        help_text="Detected RouterOS major version (6 or 7)"
    )

    # Real-time Stats
    mac_address = models.CharField(max_length=17, null=True, blank=True)
    model = models.CharField(max_length=100, null=True, blank=True)
    firmware_version = models.CharField(max_length=50, null=True, blank=True)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='offline')
    last_seen = models.DateTimeField(null=True, blank=True)
    
    total_users = models.PositiveIntegerField(default=0)
    active_users = models.PositiveIntegerField(default=0)
    uptime = models.CharField(max_length=50, null=True, blank=True)
    uptime_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    
    location = models.CharField(max_length=255, null=True, blank=True)
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    
    tags = models.JSONField(default=list, blank=True)
    notes = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # Legacy fields kept to prevent migration errors during transition
    radius_server = models.GenericIPAddressField(protocol='IPv4', null=True, blank=True)
    radius_port = models.IntegerField(default=1812)
    lan_interfaces = models.CharField(max_length=200, default='ether2,ether3')
    lan_subnet = models.CharField(max_length=20, default='192.168.88.0/24')
    hotspot_subnet = models.CharField(max_length=20, default='172.19.0.0/16')
    hotspot_portal_url = models.URLField(default='https://app.yourisp.local')
    hotspot_cookie_lifetime = models.CharField(max_length=10, default='4w2d')
    hotspot_ssl_cert = models.CharField(max_length=100, blank=True, null=True)
    sla_target = models.DecimalField(max_digits=5, decimal_places=2, default=99.00)

    class Meta:
        verbose_name = 'Router'
        verbose_name_plural = 'Routers'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['ip_address']),
            models.Index(fields=['auth_key']),
            models.Index(fields=['tenant_subdomain']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"{self.name} ({self.ip_address or 'No IP'})"

    def save(self, *args, **kwargs):
            """Auto-generate credentials and schema links."""
            
            # ────────────────────────────────────────────────────────────────
            # SAFETY CHECK: Protect the MikroTik 'admin' account!
            # ────────────────────────────────────────────────────────────────
            if not self.api_username or self.api_username.lower() == 'admin':
                self.api_username = 'netily_api'
                
            # 1. Tenant Sync
            if self.tenant_subdomain:
                clean_sub = self.tenant_subdomain.lower().replace('-', '_')
                self.schema_name = f"tenant_{clean_sub}"
            
            # 2. VPN Credentials
            if self.enable_openvpn and not self.openvpn_username:
                prefix = (self.tenant_subdomain or 'public')[:5]
                clean_name = self.name.lower().replace(' ', '')[:8]
                suffix = secrets.token_hex(2)
                self.openvpn_username = f"{prefix}_{clean_name}_{suffix}"
                self.openvpn_password = secrets.token_urlsafe(12)
            
            # 3. API Credentials
            if not self.api_password:
                self.api_password = secrets.token_urlsafe(12)

            # 4. Provision Slug (short URL-safe identifier)
            if not self.provision_slug:
                self.provision_slug = secrets.token_hex(4).lower()

            # 5. Radius Defaults
            if self.enable_openvpn and not self.radius_server:
                self.radius_server = "10.8.0.1" 

            super().save(*args, **kwargs)

    # ────────────────────────────────────────────────────────────────
    # SMART PROPERTIES (The "Brains" for the Script Generator)
    # ────────────────────────────────────────────────────────────────

    @property
    def gateway_ip(self):
        """Extracts just the IP from the CIDR (e.g., '172.18.0.1')"""
        if self.gateway_cidr and '/' in self.gateway_cidr:
            return self.gateway_cidr.split('/')[0]
        return '172.18.0.1'

    @property
    def pool_range(self):
        """Calculates IP Pool: e.g. 172.18.2.10 - 172.18.255.254"""
        ip = self.gateway_ip
        try:
            parts = ip.split('.')
            base = f"{parts[0]}.{parts[1]}"
            return f"{base}.2.10-{base}.255.254"
        except:
            return "172.18.2.10-172.18.255.254"

    # Compatibility methods
    def get_lan_ip(self): return self.gateway_ip
    def get_hotspot_ip(self): return self.gateway_ip
    def get_pppoe_local_ip(self):
        if '-' in self.pppoe_pool:
            return self.pppoe_pool.split('-')[0].rsplit('.', 1)[0] + '.1'
        return '192.40.2.1'


class RouterEvent(AuditMixin):
    # Keep existing event types...
    EVENT_TYPES = [
        ('up', 'Router Online'), ('down', 'Router Offline'), ('reboot', 'Router Rebooted'),
        ('config_change', 'Configuration Changed'), ('warning', 'Warning'), ('error', 'Error'),
        ('maintenance', 'Maintenance Mode'), ('auth_success', 'Authenticated Successfully'),
        ('auth_key_regen', 'Auth Key Regenerated'), ('backup', 'Backup Created'),
        ('user_created', 'User Created'), ('user_deleted', 'User Deleted'),
        ('user_enabled', 'User Enabled'), ('user_disabled', 'User Disabled'),
        ('queue_created', 'Queue Created'), ('queue_removed', 'Queue Removed'),
        ('interface_up', 'Interface Up'), ('interface_down', 'Interface Down'),
        ('config_sync', 'Configuration Synced'), ('script_executed', 'Script Executed'),
        ('firewall_rule_added', 'Firewall Rule Added'), ('interface_enabled', 'Interface Enabled'),
        ('interface_disabled', 'Interface Disabled'), ('queue_enabled', 'Queue Enabled'),
        ('queue_disabled', 'Queue Disabled'),
    ]

    router = models.ForeignKey(Router, on_delete=models.CASCADE, related_name='events')
    event_type = models.CharField(max_length=50, choices=EVENT_TYPES)
    message = models.TextField()
    details = models.JSONField(default=dict, blank=True, null=True)
    
    schema_name = models.SlugField(max_length=63, editable=False, null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['router', 'event_type']),
            models.Index(fields=['created_at']),
        ]
    
    def save(self, *args, **kwargs):
        if self.router and self.router.schema_name:
            self.schema_name = self.router.schema_name
        super().save(*args, **kwargs)


# ====================== SUB-MODELS (Fixed Circular Imports) ======================

class MikrotikInterface(AuditMixin):
    router = models.ForeignKey(Router, on_delete=models.CASCADE, related_name='interfaces')
    interface_name = models.CharField(max_length=50)
    interface_type = models.CharField(max_length=20, choices=[
        ('ETHERNET', 'Ethernet'), ('WLAN', 'Wireless'), ('BRIDGE', 'Bridge'),
        ('VLAN', 'VLAN'), ('PPPOE', 'PPPoE'), ('OTHER', 'Other'),
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
    schema_name = models.SlugField(max_length=63, editable=False, null=True, blank=True)

    class Meta:
        unique_together = [['router', 'interface_name']]
        ordering = ['interface_name']

    def save(self, *args, **kwargs):
        if self.router and self.router.schema_name:
            self.schema_name = self.router.schema_name
        super().save(*args, **kwargs)


class HotspotUser(AuditMixin):
    router = models.ForeignKey(Router, on_delete=models.CASCADE, related_name='hotspot_users')
    
    # FIXED: Use String Reference to avoid Circular Import
    service_connection = models.OneToOneField(
        'customers.ServiceConnection',
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
        ('ACTIVE', 'Active'), ('DISABLED', 'Disabled'),
        ('EXPIRED', 'Expired'), ('BLOCKED', 'Blocked'),
    ], default='ACTIVE')
    profile = models.CharField(max_length=100, default='default')
    
    connected_since = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    schema_name = models.SlugField(max_length=63, editable=False, null=True, blank=True)

    class Meta:
        unique_together = [['router', 'username']]
        ordering = ['username']
        indexes = [models.Index(fields=['router', 'status'])]

    def save(self, *args, **kwargs):
        if self.router and self.router.schema_name:
            self.schema_name = self.router.schema_name
        super().save(*args, **kwargs)


class PPPoEUser(AuditMixin):
    router = models.ForeignKey(Router, on_delete=models.CASCADE, related_name='pppoe_users')
    
    # FIXED: Use String Reference
    service_connection = models.OneToOneField(
        'customers.ServiceConnection',
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
        ('CONNECTED', 'Connected'), ('DISCONNECTED', 'Disconnected'),
        ('DISABLED', 'Disabled'),
    ], default='DISCONNECTED')
    profile = models.CharField(max_length=100, default='default-encryption')
    
    connected_since = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    schema_name = models.SlugField(max_length=63, editable=False, null=True, blank=True)

    class Meta:
        unique_together = [['router', 'username']]
        ordering = ['username']
        indexes = [models.Index(fields=['router', 'status'])]

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

    hotspot_user = models.ForeignKey(HotspotUser, on_delete=models.SET_NULL, null=True, blank=True)
    pppoe_user = models.ForeignKey(PPPoEUser, on_delete=models.SET_NULL, null=True, blank=True)
    schema_name = models.SlugField(max_length=63, editable=False, null=True, blank=True)

    class Meta:
        unique_together = [['router', 'queue_name']]
        ordering = ['queue_name']

    def save(self, *args, **kwargs):
        if self.router and self.router.schema_name:
            self.schema_name = self.router.schema_name
        super().save(*args, **kwargs)


class RouterConfiguration(AuditMixin):
    router = models.ForeignKey(Router, on_delete=models.CASCADE, related_name='configurations')
    config_type = models.CharField(max_length=20, choices=Router.CONFIG_TYPES)
    config_data = models.JSONField(default=dict, help_text="Configuration parameters")
    config_script = models.TextField(help_text="Generated RouterOS script")
    version = models.CharField(max_length=10, default='1.0')
    is_active = models.BooleanField(default=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    applied_by = models.CharField(max_length=100, blank=True, null=True)
    schema_name = models.SlugField(max_length=63, editable=False, null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['router', 'is_active'])]
    
    def save(self, *args, **kwargs):
        if self.router and self.router.schema_name:
            self.schema_name = self.router.schema_name
        super().save(*args, **kwargs)