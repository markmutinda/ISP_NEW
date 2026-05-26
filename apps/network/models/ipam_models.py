import ipaddress
import logging

from django.db import models
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from rest_framework.exceptions import ValidationError  # CHANGED: Using DRF ValidationError
from netaddr import IPNetwork, IPAddress as NetIPAddress
from apps.core.models import Company, AuditMixin
# REMOVED: from apps.customers.models import ServiceConnection  # ← DELETED THIS LINE
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

# CIDR mask options for pool sizing — EXPANDED
CIDR_CHOICES = [
    (16, '/16 — 65,534 usable IPs'),
    (17, '/17 — 32,766 usable IPs'),
    (18, '/18 — 16,382 usable IPs'),
    (19, '/19 — 8,190 usable IPs'),
    (20, '/20 — 4,094 usable IPs'),
    (21, '/21 — 2,046 usable IPs'),
    (22, '/22 — 1,022 usable IPs'),
    (23, '/23 — 510 usable IPs'),
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
        help_text='CIDR mask: /16=65K IPs, /24=254 IPs, /25=126, etc.'
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
        unique_together = [['name']]
        ordering = ['name']
    
    def clean(self):
        """Validate subnet prefix is not blocked and prevent overlapping pools."""
        super().clean()
        
        # Existing validation: Check blocked prefixes
        if self.subnet_prefix and self.subnet_prefix in BLOCKED_PREFIXES:
            raise DjangoValidationError({
                'subnet_prefix': f'{self.subnet_prefix}.x.x is reserved/blocked '
                                  f'(VPN/System). Choose a different prefix.'
            })

        # NEW: Validate 3rd octet is not needed for large CIDRs
        if self.subnet_prefix and self.cidr_prefix is not None:
            prefix_parts = self.subnet_prefix.split('.')
            
            if len(prefix_parts) == 3 and self.cidr_prefix < 24:
                raise DjangoValidationError({
                    'cidr_prefix': f'Prefix {self.subnet_prefix} is a /24-style prefix. '
                                   f'Use /24 or smaller, or switch to a 2-octet prefix like 10.50.'
                })
            
            # For /16 and larger with 2-octet prefix, force subnet_octet to 0
            if len(prefix_parts) == 2 and self.cidr_prefix <= 16:
                self.subnet_octet = 0  # Force it, don't let user confuse things

        # NEW: Prevent overlapping octets on the same prefix
        if self.subnet_prefix and self.subnet_octet is not None and self.cidr_prefix is not None:
            # Only check overlap for /24 and smaller where octet is meaningful
            if self.cidr_prefix >= 24:
                overlapping = IPPool.objects.filter(
                    subnet_prefix=self.subnet_prefix,
                    subnet_octet=self.subnet_octet
                ).exclude(pk=self.pk)
                
                if overlapping.exists():
                    raise DjangoValidationError({
                        'subnet_octet': f"Octet {self.subnet_octet} on prefix {self.subnet_prefix} "
                                        f"is already used by pool '{overlapping.first().name}'."
                    })
        
        # Optional: Validate that start_ip < end_ip if both are provided
        if self.start_ip and self.end_ip:
            try:
                start = ipaddress.IPv4Address(self.start_ip)
                end = ipaddress.IPv4Address(self.end_ip)
                if start >= end:
                    raise DjangoValidationError({
                        'start_ip': 'Start IP must be less than End IP',
                        'end_ip': 'End IP must be greater than Start IP'
                    })
            except ValueError:
                # IP validation will be caught by the field itself
                pass
    
    def _compute_from_subnet_builder(self):
        """Compute start_ip, end_ip, gateway from subnet builder fields.
        
        Handles CIDR values correctly:
        - For /16 or larger: network is prefix.0.0 (3rd octet forced to 0)
        - For /17 to /23: uses 3rd octet (defaults to 0 if not set)
        - For /24 or smaller: requires 3rd octet
        """
        if not (self.subnet_prefix and self.cidr_prefix):
            return  # Legacy mode — fields already set manually

        prefix_parts = self.subnet_prefix.split('.')

        if len(prefix_parts) == 2:
            # 2-octet prefix: e.g. "10.50" or "172.16"
            if self.cidr_prefix <= 16:
                # /16 or larger — 3rd octet is irrelevant, always use 0
                net_str = f"{self.subnet_prefix}.0.0/{self.cidr_prefix}"
            elif self.cidr_prefix <= 23:
                # /17-/23 — 3rd octet matters, default to 0 if not set
                octet = self.subnet_octet if self.subnet_octet is not None else 0
                net_str = f"{self.subnet_prefix}.{octet}.0/{self.cidr_prefix}"
            else:
                # /24 or smaller — 3rd octet required
                if self.subnet_octet is None:
                    logger.error(f"subnet_octet is required for /{self.cidr_prefix} pool")
                    return
                net_str = f"{self.subnet_prefix}.{self.subnet_octet}.0/{self.cidr_prefix}"
        elif len(prefix_parts) == 3:
            # 3-octet prefix: e.g. "192.168.10" — only supports /24 or smaller
            net_str = f"{self.subnet_prefix}.0/{self.cidr_prefix}"
        else:
            return

        try:
            network = ipaddress.IPv4Network(net_str, strict=False)
        except ValueError as e:
            logger.error(f"Invalid subnet: {net_str} — {e}")
            return

        first_host = network.network_address + 1
        last_host = network.broadcast_address - 1

        self.network_address = str(network.network_address)
        self.broadcast_address = str(network.broadcast_address)
        self.gateway = str(first_host)
        self.start_ip = str(first_host + 1)
        self.end_ip = str(last_host)
        self.total_ips = int(last_host) - int(first_host)
    
    def save(self, *args, **kwargs):
        # Run validation before saving
        self.full_clean()
        
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
        # For large pools, defer to background task to avoid request timeout
        if is_new and self.subnet_prefix and self.start_ip and self.end_ip:
            # For large pools, queue async task
            if self.total_ips > 1000:
                try:
                    from django.db import connection
                    from apps.network.tasks import populate_ip_pool_addresses
                    populate_ip_pool_addresses.delay(self.id, connection.schema_name)
                    logger.info(
                        f"IPPool '{self.name}': queued IP generation for {self.total_ips} addresses "
                        f"in schema '{connection.schema_name}'"
                    )
                except Exception as e:
                    logger.warning(f"Could not queue IP generation, falling back to sync: {e}")
                    self._populate_ip_addresses()
            else:
                self._populate_ip_addresses()
    
    def _populate_ip_addresses(self):
        """Generate IPAddress records or adopt orphans.
        
        Uses chunking for large pools to prevent database timeouts.
        """
        if not self.start_ip or not self.end_ip:
            return
        
        start = int(NetIPAddress(self.start_ip))
        end = int(NetIPAddress(self.end_ip))
        
        # 1. First, ADOPT any "Orphan" IPs in this range (Fixes the Empty Dropdown issue)
        # Orphans are IPs that exist but have ip_pool=None
        orphans = IPAddress.objects.filter(
            ip_pool__isnull=True
        )
        
        # We can't filter by range easily in SQL with strings, so we do a quick check
        # This is safe because orphans are usually few
        count_adopted = 0
        for orphan in orphans:
            try:
                ip_val = int(NetIPAddress(orphan.ip_address))
                if start <= ip_val <= end:
                    orphan.ip_pool = self
                    orphan.subnet = self.subnet
                    orphan.status = 'AVAILABLE'  # Reset status to be safe
                    orphan.save(update_fields=['ip_pool', 'subnet', 'status'])
                    count_adopted += 1
            except ValueError:
                continue
                
        if count_adopted > 0:
            logger.info(f"Pool '{self.name}': Adopted {count_adopted} orphaned IPs.")

        # 2. Now generate NEW IPs for gaps with chunking
        existing = set(
            IPAddress.objects.filter(ip_pool=self)
            .values_list('ip_address', flat=True)
        )
        
        new_addresses = []
        for ip_int in range(start, end + 1):
            ip_str = str(NetIPAddress(ip_int))
            # Check if it exists in THIS pool (or was just adopted)
            if ip_str not in existing:
                # Double check it doesn't exist in another pool (avoid duplicates)
                if not IPAddress.objects.filter(ip_address=ip_str).exists():
                    new_addresses.append(IPAddress(
                        ip_pool=self,
                        subnet=self.subnet,
                        ip_address=ip_str,
                        assignment_type='STATIC',
                        status='AVAILABLE',
                        description=f'Auto-generated for {self.name}',
                    ))
        
        # CHUNKED bulk_create to prevent database timeout on large pools
        if new_addresses:
            CHUNK_SIZE = 500
            created_count = 0
            for i in range(0, len(new_addresses), CHUNK_SIZE):
                chunk = new_addresses[i:i + CHUNK_SIZE]
                IPAddress.objects.bulk_create(chunk, ignore_conflicts=True)
                created_count += len(chunk)
            
            logger.info(f"IPPool '{self.name}': generated {created_count} new IP addresses "
                        f"({self.start_ip} → {self.end_ip})")
    
    def refresh_usage(self):
        """Recalculate used_ips from the IPAddress ledger."""
        self.used_ips = self.ip_addresses.filter(status='ASSIGNED').count()  # ← CHANGED from pool_addresses to ip_addresses
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
    ip_pool = models.ForeignKey(IPPool, on_delete=models.SET_NULL, null=True, blank=True, related_name='ip_addresses')  # ← CHANGED from 'pool_addresses' to 'ip_addresses'
    
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
        'customers.ServiceConnection',  # ← CHANGED to string reference
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
        
        # Logic remains the same, Python handles the object passing fine
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


# ========== SIGNAL HANDLERS ==========

@receiver(pre_delete, sender=Subnet)
def protect_subnets_in_use(sender, instance, **kwargs):
    """
    Prevent deletion of subnets that have active pools or VLANs.
    This prevents accidental cascade deletion of all associated IP pools
    and potentially customer-assigned IPs.
    """
    # Check if this subnet has any IP pools
    if instance.pools.exists():
        pool_count = instance.pools.count()
        raise ValidationError(
            f"Cannot delete Subnet '{instance.name}'. It contains {pool_count} active IP Pool(s). "
            "Please delete or reassign the pools first."
        )
    
    # Check if this subnet is linked to any VLANs
    if instance.assigned_vlans.exists():
        vlan_count = instance.assigned_vlans.count()
        raise ValidationError(
            f"Cannot delete Subnet '{instance.name}'. It is linked to {vlan_count} active VLAN(s). "
            "Please remove the VLAN associations first."
        )


@receiver(pre_delete, sender=IPPool)
def protect_active_ip_pools(sender, instance, **kwargs):
    """
    Prevent deletion of an IP Pool if:
    1. It is linked to any Billing Plan.
    2. It has any IP addresses currently ASSIGNED to users.
    """
    
    # 1. Check if any Plan is using this pool
    # We rely on the 'related_name="plans"' defined in Billing Plan model.
    linked_plans = 0
    if hasattr(instance, 'plans'):
        linked_plans = instance.plans.count()
    
    if linked_plans > 0:
        raise ValidationError(
            f"Cannot delete IP Pool '{instance.name}'. It is linked to {linked_plans} Plan(s). "
            "Please detach this pool from the plans first."
        )

    # 2. Check if any IP in this pool is currently assigned to a customer
    assigned_ips = instance.ip_addresses.filter(status='ASSIGNED').count()  # ← CHANGED from pool_addresses to ip_addresses
    
    if assigned_ips > 0:
        raise ValidationError(
            f"Cannot delete IP Pool '{instance.name}'. It has {assigned_ips} IP address(es) currently assigned to active users. "
            "Please release these IPs first."
        )