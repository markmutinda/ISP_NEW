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

def generate_api_password():
    """
    Generate a RouterOS-safe API password.
    - Max length: 20 characters (safe for v6)
    - Alphanumeric only (no special chars that might need escaping)
    """
    # Use token_hex which gives 0-9a-f only (2 chars per byte)
    return secrets.token_hex(10)  # 20 characters, hex only

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
    api_password = models.CharField(
        max_length=255, 
        blank=True, 
        default=generate_api_password,
        help_text="Auto-generated on save (RouterOS-safe, max 20 chars)"
    )
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
    
    # Hotspot IPAM Configuration (user-selectable via dropdowns)
    hotspot_base_ip = models.GenericIPAddressField(
        default='172.12.0.1',
        help_text="Gateway IP for the hotspot network (e.g., 172.12.0.1)"
    )
    hotspot_subnet_cidr = models.IntegerField(
        default=16,
        validators=[MinValueValidator(8), MaxValueValidator(30)],
        help_text="CIDR prefix length for hotspot subnet (e.g., 16 = /16 = 65,534 hosts)"
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
    openvpn_server = models.CharField(max_length=100, default='vpn.netily.co.ke')
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
    
    # ── Captive Portal Customisation (per-router) ──
    template_id = models.IntegerField(
        default=1,
        help_text="UI template for captive portal (1-7)"
    )
    hotspot_name = models.CharField(
        max_length=100, blank=True, default='',
        help_text="Display name on the captive portal (e.g. 'Mjengo Fast Wi-Fi')"
    )
    support_phone = models.CharField(
        max_length=20, blank=True, default='',
        help_text="Customer-care phone shown on captive portal"
    )
    announcement_text = models.CharField(
        max_length=255, blank=True, default='',
        help_text="Short banner message on the captive portal (e.g. 'Buy 24h for KES 50!')"
    )

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

    def sync_status(self, force=False):
        """
        Fast socket check to see if the MikroTik is reachable (1.5s max delay).
        
        Args:
            force (bool): If True, bypasses the cooldown check and forces a sync.
                          If False, only syncs if last check was more than 30 seconds ago.
        
        Returns:
            str: The updated status ('online' or 'offline')
        """
        import socket
        from django.utils import timezone
        
        # 1. COOLDOWN: Don't check if we just checked less than 30 seconds ago
        if not force and self.last_seen:
            now = timezone.now()
            diff = (now - self.last_seen).total_seconds()
            if diff < 30:  # 30-second cooldown
                return self.status

        # 2. GET IP: Use VPN IP if available, else WAN IP
        target_ip = self.vpn_ip_address if (self.vpn_provisioned and self.vpn_ip_address) else self.ip_address
        
        if not target_ip:
            self.status = 'offline'
            self.save(update_fields=['status', 'updated_at'])
            return self.status

        # 3. FAST SOCKET PING: Just check if port 8728 is open (bypasses heavy auth)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.5)  # Max 1.5 seconds wait time per router!
        
        try:
            result = sock.connect_ex((target_ip, self.api_port or 8728))
            if result == 0:
                self.status = 'online'
                self.last_seen = timezone.now()
            else:
                self.status = 'offline'
        except Exception:
            self.status = 'offline'
        finally:
            sock.close()
        
        # 4. UPDATE DB silently
        self.save(update_fields=['status', 'last_seen', 'updated_at'])
        return self.status

    def save(self, *args, **kwargs):
        """Auto-generate credentials and trigger VPN provisioning."""
        from django.utils.text import slugify
        
        # ────────────────────────────────────────────────────────────────
        # SAFETY CHECK: Protect the MikroTik 'admin' account!
        # ────────────────────────────────────────────────────────────────
        if not self.api_username or self.api_username.lower() == 'admin':
            self.api_username = 'netily_api'
            
        # 1. Tenant Sync
        if self.tenant_subdomain:
            clean_sub = self.tenant_subdomain.lower().replace('-', '_')
            self.schema_name = f"tenant_{clean_sub}"
        
        # 2. VPN Credentials — unique per router (tenant_router_vpn format)
        if self.enable_openvpn and not self.openvpn_username:
            tenant_prefix = slugify(self.tenant_subdomain or 'public').replace('-', '')[:10]
            router_prefix = slugify(self.name or 'router').replace('-', '')[:12]
            suffix = secrets.token_hex(2)
            self.openvpn_username = f"{tenant_prefix}_{router_prefix}_{suffix}_vpn"[:60]
        if self.enable_openvpn and not self.openvpn_password:
            self.openvpn_password = secrets.token_urlsafe(16)
        
        # 3. API Credentials — always ensure a strong password
        # Use the new generator function to ensure RouterOS compatibility
        if not self.api_password:
            self.api_password = generate_api_password()

        # 4. Provision Slug (short URL-safe identifier)
        if not self.provision_slug:
            self.provision_slug = secrets.token_hex(4).lower()

        # 5. RADIUS Shared Secret — unique per router
        if not self.shared_secret:
            self.shared_secret = secrets.token_hex(16)

        # 6. Radius Server Defaults
        if self.enable_openvpn and not self.radius_server:
            self.radius_server = "10.8.0.1"

        # 7. Save the router first (so it has an ID for certificate generation)
        super().save(*args, **kwargs)

        # ────────────────────────────────────────────────────────────────
        # 8. Trigger LipaNet-Style VPN Provisioning if not already done
        # ────────────────────────────────────────────────────────────────
        if self.enable_openvpn and not self.vpn_provisioned:
            try:
                from apps.vpn.services.vpn_provisioning_service import VPNProvisioningService
                service = VPNProvisioningService()
                # This will assign IP, generate certs, write CCD, and update router
                service.provision_router(self)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(
                    f"VPN Auto-Provisioning failed for {self.name}: {e}"
                )

        # 9. Update the Central RADIUS Phonebook (GlobalRouterMap)
        # We must use connection.cursor or switch schema to write to the public schema
        from django.db import connection
        from apps.core.models import GlobalRouterMap, Tenant
        
        # --- FIX A: Prioritize VPN IP for RADIUS NAS identification ---
        # RADIUS requests come from the VPN tunnel IP, not the public WAN IP
        nas_ip = self.vpn_ip_address or self.ip_address
        # -------------------------------------------------------------
        
        if nas_ip and self.tenant_subdomain:
            try:
                # Temporarily switch to public schema to save the map
                current_schema = connection.schema_name
                connection.set_schema_to_public()
                
                tenant_obj = Tenant.objects.get(subdomain=self.tenant_subdomain)
                
                GlobalRouterMap.objects.update_or_create(
                    nas_ip=nas_ip,  # Use the prioritized IP
                    defaults={
                        'nas_secret': self.shared_secret,
                        'tenant': tenant_obj,
                        'is_active': self.status == 'online' or self.is_active
                    }
                )
                
                # Switch back to the tenant's schema
                connection.set_schema(current_schema)
            except Tenant.DoesNotExist:
                # Log error but don't break the save operation
                import logging
                logging.getLogger(__name__).error(
                    f"Tenant with subdomain {self.tenant_subdomain} not found for router {self.name}"
                )
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(
                    f"Failed to update GlobalRouterMap for router {self.name}: {e}"
                )

    # ────────────────────────────────────────────────────────────────
    # SMART PROPERTIES (The "Brains" for the Script Generator)
    # ────────────────────────────────────────────────────────────────

    @property
    def gateway_ip(self):
        """Extracts the base IP from the new IPAM fields"""
        # 1. Prioritize the new dynamic field
        if self.hotspot_base_ip:
            return self.hotspot_base_ip
            
        # 2. Fallback to the old cidr field if missing
        if self.gateway_cidr and '/' in self.gateway_cidr:
            return self.gateway_cidr.split('/')[0]
            
        # 3. Ultimate default
        return '172.12.0.1'

    @property
    def pool_range(self):
        """Calculates IP Pool dynamically using the IPAM Calculator"""
        if self.hotspot_base_ip and self.hotspot_subnet_cidr:
            # Use our brilliant new calculator to get the perfect pool math
            from apps.network.services.ipam_calculator import calculate_mikrotik_hotspot_network
            math = calculate_mikrotik_hotspot_network(self.hotspot_base_ip, self.hotspot_subnet_cidr)
            return math['pool_range']
            
        # Fallback for legacy routers
        return "172.12.0.10-172.12.255.254"

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