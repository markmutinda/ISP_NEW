from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator
from netaddr import IPNetwork, IPAddress as NetIPAddress
from apps.core.models import Company, AuditMixin
from apps.customers.models import ServiceConnection


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
    """IP Pool Model for DHCP"""
    POOL_TYPE = [
        ('DHCP', 'DHCP Pool'),
        ('STATIC', 'Static Pool'),
        ('RESERVED', 'Reserved Pool'),
    ]
    
    subnet = models.ForeignKey(Subnet, on_delete=models.CASCADE, related_name='pools')
    name = models.CharField(max_length=100)
    pool_type = models.CharField(max_length=20, choices=POOL_TYPE, default='DHCP')
    start_ip = models.GenericIPAddressField(protocol='IPv4')
    end_ip = models.GenericIPAddressField(protocol='IPv4')
    gateway = models.GenericIPAddressField(protocol='IPv4', blank=True, null=True)
    dns_servers = models.CharField(max_length=200, blank=True)  # comma-separated
    lease_time = models.CharField(max_length=20, default='1d')  # e.g., 1d, 12h
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    
    # Usage
    total_ips = models.IntegerField(default=0)
    used_ips = models.IntegerField(default=0)
    
    # Tenant schema field
    schema_name = models.SlugField(
        max_length=63,
        unique=True,
        editable=False,
        default="default_schema"
    )
    
    class Meta:
        verbose_name = 'IP Pool'
        verbose_name_plural = 'IP Pools'
        unique_together = [['subnet', 'name']]
        ordering = ['name']
    
    def save(self, *args, **kwargs):
        # Calculate total IPs in range
        if self.start_ip and self.end_ip:
            start = NetIPAddress(self.start_ip)
            end = NetIPAddress(self.end_ip)
            self.total_ips = (end.value - start.value) + 1
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.name} ({self.start_ip} - {self.end_ip})"


class IPAddress(AuditMixin):
    """IP Address Assignment Model"""
    ASSIGNMENT_TYPE = [
        ('DYNAMIC', 'Dynamic'),
        ('STATIC', 'Static'),
        ('RESERVED', 'Reserved'),
        ('GATEWAY', 'Gateway'),
        ('NETWORK', 'Network'),
        ('BROADCAST', 'Broadcast'),
    ]
    
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('RESERVED', 'Reserved'),
        ('AVAILABLE', 'Available'),
        ('EXPIRED', 'Expired'),
    ]
    
    subnet = models.ForeignKey(Subnet, on_delete=models.CASCADE, related_name='ip_addresses')
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
    
    # Device info
    device_type = models.CharField(max_length=50, blank=True)
    manufacturer = models.CharField(max_length=100, blank=True)
    
    # Tenant schema field
    schema_name = models.SlugField(
        max_length=63,
        unique=True,
        editable=False,
        default="default_schema"
    )
    
    class Meta:
        verbose_name = 'IP Address'
        verbose_name_plural = 'IP Addresses'
        ordering = ['ip_address']
        indexes = [
            models.Index(fields=['ip_address']),
            models.Index(fields=['status']),
            models.Index(fields=['mac_address']),
            models.Index(fields=['service_connection']),
        ]
    
    def __str__(self):
        return f"{self.ip_address} - {self.hostname or self.description[:50]}"


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