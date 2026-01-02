# apps/network/models/olt_models.py
from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from apps.core.models import Company, AuditMixin
from apps.customers.models import ServiceConnection


class OLTDevice(AuditMixin, models.Model):
    """OLT Device Model"""
    VENDOR_CHOICES = [
        ('ZTE', 'ZTE'),
        ('HUAWEI', 'Huawei'),
        ('NOKIA', 'Nokia'),
        ('FIBERHOME', 'FiberHome'),
        ('ALU', 'Alcatel-Lucent'),
        ('OTHER', 'Other'),
    ]
    
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('MAINTENANCE', 'Maintenance'),
        ('OFFLINE', 'Offline'),
        ('DECOMMISSIONED', 'Decommissioned'),
    ]
    
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='olt_devices')
    name = models.CharField(max_length=100)
    hostname = models.CharField(max_length=200, unique=True)
    ip_address = models.GenericIPAddressField(protocol='IPv4')
    vendor = models.CharField(max_length=20, choices=VENDOR_CHOICES)
    model = models.CharField(max_length=100)
    serial_number = models.CharField(max_length=100, unique=True)
    firmware_version = models.CharField(max_length=50)
    location = models.CharField(max_length=200, blank=True)
    community_string = models.CharField(max_length=100, blank=True)  # SNMP community
    ssh_username = models.CharField(max_length=100, blank=True)
    ssh_password = models.CharField(max_length=200, blank=True)
    telnet_port = models.IntegerField(default=23, validators=[MinValueValidator(1), MaxValueValidator(65535)])
    api_port = models.IntegerField(default=8080, validators=[MinValueValidator(1), MaxValueValidator(65535)])
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    last_sync = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    
    class Meta:
        verbose_name = 'OLT Device'
        verbose_name_plural = 'OLT Devices'
        ordering = ['name']
        unique_together = [['company', 'serial_number']]
    
    def __str__(self):
        return f"{self.name} ({self.vendor})"


class OLTPort(AuditMixin, models.Model):
    """OLT Port Model (Physical Ports)"""
    PORT_TYPE_CHOICES = [
        ('UPLINK', 'Uplink'),
        ('PON', 'PON Port'),
        ('ETH', 'Ethernet'),
        ('GE', 'Gigabit Ethernet'),
        ('XE', '10G Ethernet'),
    ]
    
    olt = models.ForeignKey(OLTDevice, on_delete=models.CASCADE, related_name='ports')
    port_number = models.CharField(max_length=10)
    port_type = models.CharField(max_length=10, choices=PORT_TYPE_CHOICES)
    description = models.CharField(max_length=200, blank=True)
    admin_state = models.BooleanField(default=True)  # True = Up, False = Down
    operational_state = models.BooleanField(default=False)  # True = Up, False = Down
    speed = models.CharField(max_length=20, blank=True)  # e.g., 1G, 10G
    mtu = models.IntegerField(default=1500)
    last_change = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'OLT Port'
        verbose_name_plural = 'OLT Ports'
        unique_together = [['olt', 'port_number']]
        ordering = ['olt', 'port_number']
    
    def __str__(self):
        return f"{self.olt.name} - Port {self.port_number}"


class PONPort(AuditMixin, models.Model):
    """PON Port Model (Specifically for GPON/EPON)"""
    PON_TYPE_CHOICES = [
        ('GPON', 'GPON'),
        ('EPON', 'EPON'),
        ('XG-PON', 'XG-PON'),
        ('XGS-PON', 'XGS-PON'),
    ]
    
    olt_port = models.ForeignKey(OLTPort, on_delete=models.CASCADE, related_name='pon_ports')
    pon_index = models.CharField(max_length=20)  # e.g., 0/1/1
    pon_type = models.CharField(max_length=10, choices=PON_TYPE_CHOICES)
    splitter_ratio = models.CharField(max_length=20, default='1:32')  # e.g., 1:32, 1:64
    total_onus = models.IntegerField(default=0)
    registered_onus = models.IntegerField(default=0)
    rx_power = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    tx_power = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    distance = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)  # in meters
    status = models.CharField(max_length=20, choices=[
        ('OPERATIONAL', 'Operational'),
        ('DEGRADED', 'Degraded'),
        ('FAILED', 'Failed'),
    ], default='OPERATIONAL')
    
    class Meta:
        verbose_name = 'PON Port'
        verbose_name_plural = 'PON Ports'
        unique_together = [['olt_port', 'pon_index']]
        ordering = ['olt_port', 'pon_index']
    
    def __str__(self):
        return f"PON {self.pon_index} - {self.pon_type}"


class ONUDevice(AuditMixin, models.Model):
    """ONU Device Model"""
    ONU_TYPE_CHOICES = [
        ('HG8245H', 'Huawei HG8245H'),
        ('HG8245Q2', 'Huawei HG8245Q2'),
        ('F660', 'ZTE F660'),
        ('F601', 'ZTE F601'),
        ('AN5506', 'Nokia AN5506'),
        ('OTHER', 'Other'),
    ]
    
    STATUS_CHOICES = [
        ('REGISTERED', 'Registered'),
        ('ONLINE', 'Online'),
        ('OFFLINE', 'Offline'),
        ('LOS', 'Loss of Signal'),
        ('SUSPENDED', 'Suspended'),
    ]
    
    pon_port = models.ForeignKey(PONPort, on_delete=models.CASCADE, related_name='onus')
    service_connection = models.OneToOneField(
        ServiceConnection, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='onu_device'
    )
    serial_number = models.CharField(max_length=50, unique=True)
    mac_address = models.CharField(max_length=17, unique=True)
    onu_type = models.CharField(max_length=50, choices=ONU_TYPE_CHOICES)
    onu_index = models.CharField(max_length=20)  # e.g., 0/1/1:1
    rx_power = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    tx_power = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    distance = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='REGISTERED')
    last_seen = models.DateTimeField(null=True, blank=True)
    registration_date = models.DateTimeField(null=True, blank=True)
    config_version = models.CharField(max_length=50, blank=True)
    
    class Meta:
        verbose_name = 'ONU Device'
        verbose_name_plural = 'ONU Devices'
        ordering = ['pon_port', 'onu_index']
    
    def __str__(self):
        return f"ONU {self.serial_number[:8]} - {self.service_connection.customer.full_name if self.service_connection else 'Unassigned'}"


class OLTConfig(AuditMixin, models.Model):
    """OLT Configuration Snapshot"""
    olt = models.ForeignKey(OLTDevice, on_delete=models.CASCADE, related_name='configs')
    config_type = models.CharField(max_length=20, choices=[
        ('RUNNING', 'Running Config'),
        ('STARTUP', 'Startup Config'),
        ('BACKUP', 'Backup Config'),
    ])
    version = models.CharField(max_length=50)
    config_data = models.TextField()
    checksum = models.CharField(max_length=64)  # SHA256 hash
    applied_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    applied_date = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        verbose_name = 'OLT Configuration'
        verbose_name_plural = 'OLT Configurations'
        ordering = ['-applied_date']
    
    def __str__(self):
        return f"{self.olt.name} - {self.config_type} v{self.version}"