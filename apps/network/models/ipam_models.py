import ipaddress
import logging

from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from netaddr import IPNetwork, IPAddress as NetIPAddress
from apps.core.models import Company, AuditMixin
from apps.customers.models import ServiceConnection
from apps.network.models.router_models import Router

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────
# Subnet Prefix Choices — Cloud-Led IPAM
# Mark's spec: Allow 10.x (NOT 8 or 10), 172.x (16-31 NOT 18)
# ────────────────────────────────────────────────────────────────
SUBNET_PREFIX_CHOICES = [
    # 10.x.x.x — Main ISP ranges (exclude 10.8 VPN, 10.10 reserved)
    ('10.0', '10.0.x.x'),
    ('10.20', '10.20.x.x'),
    ('10.30', '10.30.x.x'),
    ('10.40', '10.40.x.x'),
    ('10.50', '10.50.x.x (Netily Standard)'),
    ('10.60', '10.60.x.x'),
    ('10.70', '10.70.x.x'),
    ('10.80', '10.80.x.x'),
    ('10.90', '10.90.x.x'),
    ('10.100', '10.100.x.x'),
    ('10.110', '10.110.x.x'),
    ('10.120', '10.120.x.x'),
    # 172.16-31.x.x — exclude 172.18 (Docker)
    ('172.16', '172.16.x.x'),
    ('172.17', '172.17.x.x'),
    ('172.19', '172.19.x.x'),
    ('172.20', '172.20.x.x'),
    ('172.24', '172.24.x.x'),
    ('172.28', '172.28.x.x'),
    ('172.31', '172.31.x.x'),
    # 192.168.x.x — May conflict with home routers
    ('192.168.0', '192.168.0.x (⚠ Home conflict)'),
    ('192.168.1', '192.168.1.x (⚠ Home conflict)'),
    ('192.168.10', '192.168.10.x'),
    ('192.168.100', '192.168.100.x'),
]

# CIDR mask options for pool sizing
CIDR_CHOICES = [
    (24, '/24 — 254 usable IPs'),
    (25, '/25 — 126 usable IPs'),
    (26, '/26 — 62 usable IPs'),
    (27, '/27 — 30 usable IPs'),
    (28, '/28 — 14 usable IPs'),
    (29, '/29 — 6 usable IPs'),
    (30, '/30 — 2 usable IPs'),
]

# Blocked prefixes per Mark's spec
BLOCKED_PREFIXES = {'10.8', '10.10', '172.18'}


class Subnet(AuditMixin):
    """IP Subnet Model"""
    VERSION_CHOICES = [
        ('IPv4', 'IPv4'),
        ('IPv6', 'IPv6'),
    ]
    
    name = models.CharField(max_length=100)
    network_address = models.GenericIPAddressField(protocol='IPv4')
    subnet_mask = models.GenericIPAddressField(protocol='IPv4')
    cidr = models.CharField(max_length=3)  # e.g., 24, 30
    version = models.CharField(max_length=4, choices=VERSION_CHOICES, default='IPv4')
    description = models.TextField(blank=True)
    vlan_id = models.IntegerField(null=True, blank=True, validators=[MaxValueValidator(4095)])
    location = models.CharField(max_length=200, blank=True)
    is_public = models.BooleanField(default=False)
    
    # Usage tracking
    total_ips = models.IntegerField(default=0)
    used_ips = models.IntegerField(default=0)
    available_ips = models.IntegerField(default=0)
    utilization_percentage = models.FloatField(default=0.0)
    
    # Tenant schema field
    schema_name = models.SlugField(
        max_length=63,
        unique=True,
        editable=False,
        default="default_schema"
    )
    
    class Meta:
        verbose_name = 'Subnet'
        verbose_name_plural = 'Subnets'
        unique_together = [['network_address', 'cidr']]
        ordering = ['network_address']
    
    def save(self, *args, **kwargs):
        # Calculate network details
        if self.network_address and self.cidr:
            network = IPNetwork(f"{self.network_address}/{self.cidr}")
            self.total_ips = network.size - 2  # Exclude network and broadcast
            self.available_ips = self.total_ips - self.used_ips
            if self.total_ips > 0:
                self.utilization_percentage = (self.used_ips / self.total_ips) * 100
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.name} ({self.network_address}/{self.cidr})"


class VLAN(AuditMixin):
    """VLAN Model"""
    vlan_id = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(4095)])
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    subnet = models.ForeignKey(Subnet, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_vlans')
    
    # Tenant schema field
    schema_name = models.SlugField(
        max_length=63,
        unique=True,
        editable=False,
        default="default_schema"
    )
    
    class Meta:
        verbose_name = 'VLAN'
        verbose_name_plural = 'VLANs'
        unique_together = [['vlan_id']]
        ordering = ['vlan_id']
    
    def __str__(self):
        return f"VLAN {self.vlan_id} - {self.name}"


class IPPool(AuditMixin):
    """IP Pool Model — Cloud-Led IPAM.
    
    Supports two modes:
    1. Legacy: Manual start_ip / end_ip
    2. Cloud-Led (LipaNet parity): subnet_prefix + subnet_octet + cidr_prefix
       → auto-generates gateway, start_ip, end_ip, and IPAddress records.
    """
    POOL_TYPE = [
        ('DHCP', 'DHCP Pool'),
        ('STATIC', 'Static Pool'),
        ('RESERVED', 'Reserved Pool'),
        ('PPPOE', 'PPPoE Pool'),
    ]
    
    subnet = models.ForeignKey(Subnet, on_delete=models.CASCADE, related_name='pools',
                               null=True, blank=True,
                               help_text='Optional parent subnet (for DHCP/Static pools)')
    # Router (NAS) this pool belongs to — the critical multi-router link
    router = models.ForeignKey(
        Router,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='ip_pools',
        help_text='The physical router (NAS) this pool exists on'
    )
    name = models.CharField(
        max_length=100,
        help_text='Pool name (auto-generated from plan name if created via plan form)'
    )
    pool_type = models.CharField(max_length=20, choices=POOL_TYPE, default='PPPOE')
    
    # ── Cloud-Led Subnet Builder Fields ──
    subnet_prefix = models.CharField(
        max_length=15,
        blank=True,
        choices=SUBNET_PREFIX_CHOICES,
        help_text='First two octets, e.g. 10.50 → 10.50.x.x'
    )
    subnet_octet = models.IntegerField(
        null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(255)],
        help_text='Third octet (0-255), e.g. 3 → 10.50.3.x'
    )
    cidr_prefix = models.IntegerField(
        null=True, blank=True,
        choices=CIDR_CHOICES,
        default=24,
        help_text='CIDR mask: /24=254 IPs, /25=126, /26=62, etc.'
    )
    
    # ── Computed / Legacy fields ──
    start_ip = models.GenericIPAddressField(protocol='IPv4', blank=True, null=True)
    end_ip = models.GenericIPAddressField(protocol='IPv4', blank=True, null=True)
    gateway = models.GenericIPAddressField(protocol='IPv4', blank=True, null=True)
    network_address = models.GenericIPAddressField(protocol='IPv4', blank=True, null=True,
                                                    help_text='Network address (e.g. 10.50.3.0)')
    broadcast_address = models.GenericIPAddressField(protocol='IPv4', blank=True, null=True,
                                                     help_text='Broadcast address (e.g. 10.50.3.255)')
    dns_servers = models.CharField(max_length=200, blank=True, default='8.8.8.8,8.8.4.4')
    lease_time = models.CharField(max_length=20, default='1d')
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    
    # Usage
    total_ips = models.IntegerField(default=0)
    used_ips = models.IntegerField(default=0)
    
    class Meta:
        verbose_name = 'IP Pool'
        verbose_name_plural = 'IP Pools'
        unique_together = [['router', 'name']]
        ordering = ['router', 'name']
    
    def clean(self):
        """Validate subnet prefix is not blocked."""
        super().clean()
        if self.subnet_prefix and self.subnet_prefix in BLOCKED_PREFIXES:
            raise ValidationError({
                'subnet_prefix': f'{self.subnet_prefix}.x.x is reserved/blocked '
                                  f'(VPN/System). Choose a different prefix.'
            })
    
    def _compute_from_subnet_builder(self):
        """Compute start_ip, end_ip, gateway, network/broadcast from subnet builder fields."""
        if not (self.subnet_prefix and self.subnet_octet is not None and self.cidr_prefix):
            return  # Legacy mode — fields already set manually
        
        # Build the network string, e.g. "10.50.3.0/24"
        prefix_parts = self.subnet_prefix.split('.')
        if len(prefix_parts) == 2:
            # e.g. "10.50" + octet 3 → "10.50.3.0"
            net_str = f"{self.subnet_prefix}.{self.subnet_octet}.0/{self.cidr_prefix}"
        elif len(prefix_parts) == 3:
            # e.g. "192.168.10" + octet ignored or used differently
            # Here octet is the 4th-octet base, but for /24+ we use 0
            net_str = f"{self.subnet_prefix}.0/{self.cidr_prefix}"
        else:
            return
        
        try:
            network = ipaddress.IPv4Network(net_str, strict=False)
        except ValueError as e:
            logger.error(f"Invalid subnet: {net_str} — {e}")
            return
        
        hosts = list(network.hosts())  # Excludes network & broadcast
        if not hosts:
            return
        
        self.network_address = str(network.network_address)
        self.broadcast_address = str(network.broadcast_address)
        self.gateway = str(hosts[0])       # .1 = gateway
        self.start_ip = str(hosts[1])      # .2 = first usable
        self.end_ip = str(hosts[-1])       # .254 = last usable
        self.total_ips = len(hosts) - 1    # Exclude gateway
    
    def save(self, *args, **kwargs):
        # If subnet builder fields are set, compute the range
        self._compute_from_subnet_builder()
        
        # Fallback: compute total from start/end if not set by builder
        if self.start_ip and self.end_ip and not self.total_ips:
            start = NetIPAddress(self.start_ip)
            end = NetIPAddress(self.end_ip)
            self.total_ips = (end.value - start.value) + 1
        
        is_new = self._state.adding
        super().save(*args, **kwargs)
        
        # Auto-generate IPAddress records for new cloud-led pools
        if is_new and self.subnet_prefix and self.start_ip and self.end_ip:
            self._populate_ip_addresses()
    
    def _populate_ip_addresses(self):
        """Generate IPAddress records for every usable IP in this pool."""
        if not self.start_ip or not self.end_ip:
            return
        
        start = int(NetIPAddress(self.start_ip))
        end = int(NetIPAddress(self.end_ip))
        
        existing = set(
            IPAddress.objects.filter(ip_pool=self)
            .values_list('ip_address', flat=True)
        )
        
        new_addresses = []
        for ip_int in range(start, end + 1):
            ip_str = str(NetIPAddress(ip_int))
            if ip_str not in existing:
                new_addresses.append(IPAddress(
                    ip_pool=self,
                    ip_address=ip_str,
                    assignment_type='STATIC',
                    status='AVAILABLE',
                    description=f'Auto-generated for {self.name}',
                ))
        
        if new_addresses:
            # bulk_create with ignore_conflicts in case IPs already exist (unique)
            IPAddress.objects.bulk_create(new_addresses, ignore_conflicts=True)
            logger.info(f"IPPool '{self.name}': generated {len(new_addresses)} IP addresses "
                        f"({self.start_ip} → {self.end_ip})")
    
    def refresh_usage(self):
        """Recalculate used_ips from the IPAddress ledger."""
        self.used_ips = self.pool_addresses.filter(status='ASSIGNED').count()
        self.save(update_fields=['used_ips'])
    
    @property
    def available_ips(self):
        return max(0, self.total_ips - self.used_ips)
    
    @property
    def utilization_percentage(self):
        if self.total_ips <= 0:
            return 0.0
        return round((self.used_ips / self.total_ips) * 100, 1)
    
    @property
    def ip_range(self):
        if self.start_ip and self.end_ip:
            return f"{self.start_ip} - {self.end_ip}"
        return ''
    
    @property
    def cidr_notation(self):
        """Return e.g. 10.50.3.0/24"""
        if self.network_address and self.cidr_prefix:
            return f"{self.network_address}/{self.cidr_prefix}"
        return ''
    
    def __str__(self):
        router_name = self.router.name if self.router else 'No Router'
        return f"{self.name} @ {router_name} ({self.ip_range or 'empty'})"


class IPAddress(AuditMixin):
    """IP Address Ledger — tracks every single IP to prevent duplicates.
    
    Cloud-Led IPAM: Each IP has a status (AVAILABLE → ASSIGNED → AVAILABLE).
    When a customer gets a PPPoE service, an AVAILABLE IP is marked ASSIGNED
    and linked to their service_connection.
    """
    ASSIGNMENT_TYPE = [
        ('DYNAMIC', 'Dynamic'),
        ('STATIC', 'Static'),
        ('RESERVED', 'Reserved'),
        ('GATEWAY', 'Gateway'),
        ('NETWORK', 'Network'),
        ('BROADCAST', 'Broadcast'),
    ]
    
    STATUS_CHOICES = [
        ('AVAILABLE', 'Available'),
        ('ASSIGNED', 'Assigned'),        # ← New: IP is in use by a customer
        ('RESERVED', 'Reserved'),
        ('ACTIVE', 'Active'),            # Legacy compat
        ('EXPIRED', 'Expired'),
    ]
    
    subnet = models.ForeignKey(Subnet, on_delete=models.CASCADE, related_name='ip_addresses',
                               null=True, blank=True)
    ip_pool = models.ForeignKey(IPPool, on_delete=models.SET_NULL, null=True, blank=True, related_name='pool_addresses')
    
    # Address details
    ip_address = models.GenericIPAddressField(protocol='IPv4', unique=True)
    assignment_type = models.CharField(max_length=20, choices=ASSIGNMENT_TYPE, default='DYNAMIC')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='AVAILABLE')
    
    # Assignment details
    mac_address = models.CharField(max_length=17, blank=True)
    hostname = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    
    # Relationships
    service_connection = models.ForeignKey(
        ServiceConnection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ip_addresses'
    )
    
    # DHCP/Lease info
    lease_start = models.DateTimeField(null=True, blank=True)
    lease_end = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    
    # Customer assignment (Cloud-Led: who currently holds this IP)
    assigned_to = models.ForeignKey(
        'customers.Customer',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='assigned_ips',
        help_text='Customer currently holding this IP'
    )
    
    # Device info
    device_type = models.CharField(max_length=50, blank=True)
    manufacturer = models.CharField(max_length=100, blank=True)
    
    class Meta:
        verbose_name = 'IP Address'
        verbose_name_plural = 'IP Addresses'
        ordering = ['ip_address']
        indexes = [
            models.Index(fields=['ip_address']),
            models.Index(fields=['status']),
            models.Index(fields=['mac_address']),
            models.Index(fields=['service_connection']),
            models.Index(fields=['ip_pool', 'status']),
        ]
    
    def assign_to_customer(self, customer, service_connection=None):
        """Mark this IP as ASSIGNED to a customer."""
        self.status = 'ASSIGNED'
        self.assigned_to = customer
        self.assignment_type = 'STATIC'
        if service_connection:
            self.service_connection = service_connection
        self.save(update_fields=['status', 'assigned_to', 'assignment_type', 'service_connection', 'updated_at'])
        # Refresh the pool's usage count
        if self.ip_pool:
            self.ip_pool.refresh_usage()
    
    def release(self):
        """Release this IP back to the pool."""
        self.status = 'AVAILABLE'
        self.assigned_to = None
        self.service_connection = None
        self.assignment_type = 'STATIC'
        self.save(update_fields=['status', 'assigned_to', 'service_connection', 'assignment_type', 'updated_at'])
        if self.ip_pool:
            self.ip_pool.refresh_usage()
    
    def __str__(self):
        label = self.hostname or (self.description[:50] if self.description else '')
        return f"{self.ip_address} [{self.status}]{f' - {label}' if label else ''}"


class DHCPRange(AuditMixin):
    """DHCP Range Configuration"""
    ip_pool = models.ForeignKey(IPPool, on_delete=models.CASCADE, related_name='dhcp_ranges')
    name = models.CharField(max_length=100)
    start_ip = models.GenericIPAddressField(protocol='IPv4')
    end_ip = models.GenericIPAddressField(protocol='IPv4')
    router = models.GenericIPAddressField(protocol='IPv4', blank=True, null=True)
    dns_server = models.GenericIPAddressField(protocol='IPv4', blank=True, null=True)
    domain_name = models.CharField(max_length=200, blank=True)
    lease_time = models.CharField(max_length=20, default='86400')  # in seconds
    is_active = models.BooleanField(default=True)
    
    # Tenant schema field
    schema_name = models.SlugField(
        max_length=63,
        unique=True,
        editable=False,
        default="default_schema"
    )
    
    class Meta:
        verbose_name = 'DHCP Range'
        verbose_name_plural = 'DHCP Ranges'
        unique_together = [['ip_pool', 'name']]
        ordering = ['start_ip']
    
    def __str__(self):
        return f"{self.name} ({self.start_ip} - {self.end_ip})"