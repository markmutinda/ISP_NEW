# apps/network/models/tr069_models.py
from django.db import models
from django.conf import settings
from apps.core.models import  Company, AuditMixin
from apps.customers.models import ServiceConnection


class ACSConfiguration(AuditMixin, models.Model):
    """ACS (Auto Configuration Server) Configuration"""
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='acs_configs')
    name = models.CharField(max_length=100)
    acs_url = models.URLField()
    acs_username = models.CharField(max_length=100, blank=True)
    acs_password = models.CharField(max_length=200, blank=True)
    connection_request_url = models.URLField(blank=True)
    cpe_username = models.CharField(max_length=100, default='tr069')
    cpe_password = models.CharField(max_length=200)
    periodic_interval = models.IntegerField(default=86400)  # in seconds
    is_active = models.BooleanField(default=True)
    
    class Meta:
        verbose_name = 'ACS Configuration'
        verbose_name_plural = 'ACS Configurations'
    
    def __str__(self):
        return f"{self.name} ({self.acs_url})"


class CPEDevice(AuditMixin, models.Model):
    """CPE Device Model (TR-069 Enabled)"""
    MANUFACTURER_CHOICES = [
        ('HUAWEI', 'Huawei'),
        ('ZTE', 'ZTE'),
        ('TP-LINK', 'TP-Link'),
        ('D-LINK', 'D-Link'),
        ('CISCO', 'Cisco'),
        ('OTHER', 'Other'),
    ]
    
    CONNECTION_STATUS = [
        ('CONNECTED', 'Connected'),
        ('DISCONNECTED', 'Disconnected'),
        ('BOOTING', 'Booting'),
        ('PROVISIONING', 'Provisioning'),
        ('ERROR', 'Error'),
    ]
    
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='cpe_devices')
    service_connection = models.OneToOneField(
        ServiceConnection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cpe_device'
    )
    acs_config = models.ForeignKey(ACSConfiguration, on_delete=models.SET_NULL, null=True)
    
    # Device Identification
    manufacturer = models.CharField(max_length=50, choices=MANUFACTURER_CHOICES)
    model = models.CharField(max_length=100)
    serial_number = models.CharField(max_length=100, unique=True)
    product_class = models.CharField(max_length=100, blank=True)
    hardware_version = models.CharField(max_length=50, blank=True)
    software_version = models.CharField(max_length=50, blank=True)
    
    # TR-069 Parameters
    oui = models.CharField(max_length=6, blank=True)  # Organizationally Unique Identifier
    cpe_id = models.CharField(max_length=200, unique=True)  # Full TR-069 CPE ID
    
    # Connection Information
    connection_status = models.CharField(max_length=20, choices=CONNECTION_STATUS, default='DISCONNECTED')
    wan_ip = models.GenericIPAddressField(protocol='IPv4', null=True, blank=True)
    wan_mac = models.CharField(max_length=17, blank=True)
    lan_ip = models.GenericIPAddressField(protocol='IPv4', default='192.168.1.1')
    last_connection = models.DateTimeField(null=True, blank=True)
    last_boot = models.DateTimeField(null=True, blank=True)
    
    # Provisioning
    provisioned = models.BooleanField(default=False)
    configuration_file = models.TextField(blank=True)  # TR-069 config file
    custom_parameters = models.JSONField(default=dict, blank=True)
    
    class Meta:
        verbose_name = 'CPE Device'
        verbose_name_plural = 'CPE Devices'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['serial_number']),
            models.Index(fields=['connection_status']),
            models.Index(fields=['last_connection']),
        ]
    
    def __str__(self):
        return f"{self.manufacturer} {self.model} - {self.serial_number[:8]}"


class TR069Parameter(AuditMixin, models.Model):
    """TR-069 Parameter Model"""
    PARAMETER_TYPE = [
        ('STRING', 'String'),
        ('INT', 'Integer'),
        ('UNSIGNED_INT', 'Unsigned Integer'),
        ('BOOLEAN', 'Boolean'),
        ('DATETIME', 'DateTime'),
        ('BASE64', 'Base64'),
        ('HEX_BINARY', 'Hex Binary'),
    ]
    
    ACCESS_TYPE = [
        ('READ_ONLY', 'Read Only'),
        ('READ_WRITE', 'Read Write'),
        ('WRITE_ONLY', 'Write Only'),
    ]
    
    cpe_device = models.ForeignKey(CPEDevice, on_delete=models.CASCADE, related_name='parameters')
    parameter_name = models.CharField(max_length=500)  # Full parameter path
    parameter_type = models.CharField(max_length=20, choices=PARAMETER_TYPE, default='STRING')
    access_type = models.CharField(max_length=20, choices=ACCESS_TYPE, default='READ_ONLY')
    current_value = models.TextField(blank=True)
    configured_value = models.TextField(blank=True)
    min_value = models.CharField(max_length=50, blank=True)
    max_value = models.CharField(max_length=50, blank=True)
    default_value = models.TextField(blank=True)
    notification = models.IntegerField(default=0)  # 0=Off, 1=Passive, 2=Active
    description = models.TextField(blank=True)
    last_updated = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'TR-069 Parameter'
        verbose_name_plural = 'TR-069 Parameters'
        unique_together = [['cpe_device', 'parameter_name']]
        ordering = ['parameter_name']
    
    def __str__(self):
        return f"{self.parameter_name.split('.')[-1]}"


class TR069Session(AuditMixin, models.Model):
    """TR-069 Session Log"""
    SESSION_TYPE = [
        ('INFORM', 'Inform'),
        ('GET_PARAMETER_VALUES', 'Get Parameter Values'),
        ('SET_PARAMETER_VALUES', 'Set Parameter Values'),
        ('GET_RPC_METHODS', 'Get RPC Methods'),
        ('DOWNLOAD', 'Download'),
        ('UPLOAD', 'Upload'),
        ('REBOOT', 'Reboot'),
        ('FACTORY_RESET', 'Factory Reset'),
    ]
    
    STATUS_CHOICES = [
        ('SUCCESS', 'Success'),
        ('FAILED', 'Failed'),
        ('PENDING', 'Pending'),
        ('TIMEOUT', 'Timeout'),
    ]
    
    cpe_device = models.ForeignKey(CPEDevice, on_delete=models.CASCADE, related_name='sessions')
    session_type = models.CharField(max_length=30, choices=SESSION_TYPE)
    session_id = models.CharField(max_length=100, unique=True)
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    duration = models.DurationField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    request_data = models.JSONField(null=True, blank=True)
    response_data = models.JSONField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    
    class Meta:
        verbose_name = 'TR-069 Session'
        verbose_name_plural = 'TR-069 Sessions'
        ordering = ['-start_time']
        indexes = [
            models.Index(fields=['session_id']),
            models.Index(fields=['start_time']),
            models.Index(fields=['status']),
        ]
    
    def __str__(self):
        return f"{self.session_type} - {self.cpe_device.serial_number[:8]}"