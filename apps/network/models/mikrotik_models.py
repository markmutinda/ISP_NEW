# apps/network/models/mikrotik_models.py
from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from apps.core.models import Company, AuditMixin
from apps.customers.models import ServiceConnection, Customer


class MikrotikDevice(AuditMixin, models.Model):
    """Mikrotik Router/Switch Model"""
    DEVICE_TYPE = [
        ('ROUTER', 'Router'),
        ('SWITCH', 'Switch'),
        ('CAP', 'CAPsMAN'),
        ('WIFI', 'Wireless'),
        ('OTHER', 'Other'),
    ]
    
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('MAINTENANCE', 'Maintenance'),
        ('OFFLINE', 'Offline'),
        ('DECOMMISSIONED', 'Decommissioned'),
    ]
    
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='mikrotik_devices')
    name = models.CharField(max_length=100)
    hostname = models.CharField(max_length=200)
    ip_address = models.GenericIPAddressField(protocol='IPv4', unique=True)
    api_port = models.IntegerField(default=8728, validators=[MinValueValidator(1), MaxValueValidator(65535)])
    ssh_port = models.IntegerField(default=22, validators=[MinValueValidator(1), MaxValueValidator(65535)])
    winbox_port = models.IntegerField(default=8291, validators=[MinValueValidator(1), MaxValueValidator(65535)])
    device_type = models.CharField(max_length=20, choices=DEVICE_TYPE, default='ROUTER')
    model = models.CharField(max_length=100)
    serial_number = models.CharField(max_length=100, unique=True)
    firmware_version = models.CharField(max_length=50)
    location = models.CharField(max_length=200, blank=True)
    
    # Authentication
    api_username = models.CharField(max_length=100, default='admin')
    api_password = models.CharField(max_length=200)
    ssh_username = models.CharField(max_length=100, blank=True)
    ssh_password = models.CharField(max_length=200, blank=True)
    
    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    last_sync = models.DateTimeField(null=True, blank=True)
    cpu_load = models.FloatField(null=True, blank=True)
    memory_usage = models.FloatField(null=True, blank=True)  # Percentage
    disk_usage = models.FloatField(null=True, blank=True)    # Percentage
    uptime = models.DurationField(null=True, blank=True)
    notes = models.TextField(blank=True)
    
    class Meta:
        verbose_name = 'Mikrotik Device'
        verbose_name_plural = 'Mikrotik Devices'
        ordering = ['name']
    
    def __str__(self):
        return f"{self.name} ({self.ip_address})"


class MikrotikInterface(AuditMixin, models.Model):
    """Mikrotik Interface Model"""
    INTERFACE_TYPE = [
        ('ETHERNET', 'Ethernet'),
        ('WLAN', 'Wireless'),
        ('BRIDGE', 'Bridge'),
        ('VLAN', 'VLAN'),
        ('PPPOE', 'PPPoE'),
        ('L2TP', 'L2TP'),
        ('SSTP', 'SSTP'),
        ('OVPN', 'OpenVPN'),
        ('GRE', 'GRE'),
        ('IPIP', 'IPIP'),
        ('BONDING', 'Bonding'),
    ]
    
    mikrotik = models.ForeignKey(MikrotikDevice, on_delete=models.CASCADE, related_name='interfaces')
    interface_name = models.CharField(max_length=50)
    interface_type = models.CharField(max_length=20, choices=INTERFACE_TYPE)
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
    
    class Meta:
        verbose_name = 'Mikrotik Interface'
        verbose_name_plural = 'Mikrotik Interfaces'
        unique_together = [['mikrotik', 'interface_name']]
        ordering = ['interface_name']
    
    def __str__(self):
        return f"{self.mikrotik.name} - {self.interface_name}"


class HotspotUser(AuditMixin, models.Model):
    """Mikrotik Hotspot User"""
    mikrotik = models.ForeignKey(MikrotikDevice, on_delete=models.CASCADE, related_name='hotspot_users')
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
    packets_in = models.BigIntegerField(default=0)
    packets_out = models.BigIntegerField(default=0)
    session_time = models.DurationField(null=True, blank=True)
    idle_time = models.DurationField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=[
        ('ACTIVE', 'Active'),
        ('DISABLED', 'Disabled'),
        ('EXPIRED', 'Expired'),
        ('BLOCKED', 'Blocked'),
    ], default='ACTIVE')
    profile = models.CharField(max_length=100, default='default')
    limit_uptime = models.DurationField(null=True, blank=True)
    limit_bytes_in = models.BigIntegerField(null=True, blank=True)
    limit_bytes_out = models.BigIntegerField(null=True, blank=True)
    last_login = models.DateTimeField(null=True, blank=True)
    last_logout = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        verbose_name = 'Hotspot User'
        verbose_name_plural = 'Hotspot Users'
        unique_together = [['mikrotik', 'username']]
        ordering = ['username']
        indexes = [
            models.Index(fields=['username']),
            models.Index(fields=['status']),
            models.Index(fields=['last_login']),
        ]
    
    def __str__(self):
        return f"{self.username}@{self.mikrotik.name}"


class PPPoEUser(AuditMixin, models.Model):
    """Mikrotik PPPoE User"""
    mikrotik = models.ForeignKey(MikrotikDevice, on_delete=models.CASCADE, related_name='pppoe_users')
    service_connection = models.OneToOneField(
        ServiceConnection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pppoe_user'
    )
    username = models.CharField(max_length=100)
    password = models.CharField(max_length=100)
    service = models.CharField(max_length=50, default='pppoe')
    caller_id = models.CharField(max_length=100, blank=True)
    local_address = models.GenericIPAddressField(protocol='IPv4', null=True, blank=True)
    remote_address = models.GenericIPAddressField(protocol='IPv4', null=True, blank=True)
    bytes_in = models.BigIntegerField(default=0)
    bytes_out = models.BigIntegerField(default=0)
    packets_in = models.BigIntegerField(default=0)
    packets_out = models.BigIntegerField(default=0)
    session_time = models.DurationField(null=True, blank=True)
    idle_time = models.DurationField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=[
        ('CONNECTED', 'Connected'),
        ('DISCONNECTED', 'Disconnected'),
        ('DISABLED', 'Disabled'),
        ('BLOCKED', 'Blocked'),
    ], default='DISCONNECTED')
    profile = models.CharField(max_length=100, default='default-encryption')
    last_connection = models.DateTimeField(null=True, blank=True)
    last_disconnection = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        verbose_name = 'PPPoE User'
        verbose_name_plural = 'PPPoE Users'
        unique_together = [['mikrotik', 'username']]
        ordering = ['username']
    
    def __str__(self):
        return f"{self.username}@{self.mikrotik.name}"


class MikrotikQueue(AuditMixin, models.Model):
    """Mikrotik Queue (Bandwidth Limitation)"""
    QUEUE_TYPE = [
        ('PCQ', 'PCQ - Per Connection Queue'),
        ('SIMPLE', 'Simple Queue'),
        ('FQ_CODEL', 'FQ-CoDel'),
        ('SFQ', 'Stochastic Fairness Queueing'),
    ]
    
    mikrotik = models.ForeignKey(MikrotikDevice, on_delete=models.CASCADE, related_name='queues')
    queue_name = models.CharField(max_length=100)
    queue_type = models.CharField(max_length=20, choices=QUEUE_TYPE, default='SIMPLE')
    target = models.CharField(max_length=200)  # IP address, subnet, or interface
    max_limit = models.CharField(max_length=50, default='10M/10M')  # e.g., 10M/10M
    burst_limit = models.CharField(max_length=50, blank=True)
    burst_threshold = models.CharField(max_length=50, blank=True)
    burst_time = models.CharField(max_length=50, blank=True)
    priority = models.IntegerField(default=8, validators=[MinValueValidator(1), MaxValueValidator(8)])
    packet_mark = models.CharField(max_length=100, blank=True)
    disabled = models.BooleanField(default=False)
    comment = models.TextField(blank=True)
    
    # Linked users
    hotspot_user = models.ForeignKey(
        HotspotUser, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='queues'
    )
    pppoe_user = models.ForeignKey(
        PPPoEUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='queues'
    )
    
    class Meta:
        verbose_name = 'Mikrotik Queue'
        verbose_name_plural = 'Mikrotik Queues'
        unique_together = [['mikrotik', 'queue_name']]
        ordering = ['queue_name']
    
    def __str__(self):
        return f"{self.queue_name} - {self.mikrotik.name}"