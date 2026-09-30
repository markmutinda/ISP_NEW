import uuid

from django.db import models


class CPEDevice(models.Model):
    STATUS_CHOICES = (
        ('never_seen', 'Never seen'),
        ('online', 'Online'),
        ('not_answering', 'Not answering'),
    )

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    label = models.CharField(max_length=100, blank=True, default='')
    serial_number = models.CharField(max_length=255, unique=True, db_index=True)

    genieacs_device_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    acs_username = models.CharField(max_length=64, unique=True)
    acs_password = models.CharField(max_length=64)

    manufacturer = models.CharField(max_length=128, blank=True, default='')
    model_name = models.CharField(max_length=128, blank=True, default='')
    oui = models.CharField(max_length=32, blank=True, default='')
    product_class = models.CharField(max_length=128, blank=True, default='')
    software_version = models.CharField(max_length=64, blank=True, default='')
    hardware_version = models.CharField(max_length=64, blank=True, default='')
    data_model = models.CharField(max_length=10, blank=True, default='')  # 'tr098' | 'tr181'

    customer = models.ForeignKey(
        'customers.Customer', on_delete=models.SET_NULL, null=True, blank=True, related_name='cpe_devices'
    )
    service_connection = models.ForeignKey(
        'customers.ServiceConnection', on_delete=models.SET_NULL, null=True, blank=True, related_name='cpe_devices'
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='never_seen')
    weak_signal = models.BooleanField(default=False)
    rx_power_dbm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    tx_power_dbm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    wan_ip = models.GenericIPAddressField(null=True, blank=True)
    uptime_seconds = models.BigIntegerField(null=True, blank=True)
    inform_interval = models.IntegerField(null=True, blank=True)

    # wifi_networks: [{"id": "1-1", "ssid": "...", "enabled": true, "band": "2.4GHz",
    #                   "channel": 6, "standard": "b,g,n", "clients": 3,
    #                   "_paths": {"ssid": "...", "password": "...", "enable": "..."}}]
    wifi_networks = models.JSONField(default=list, blank=True)
    # hosts: [{"name": "...", "ip": "...", "mac": "...", "type": "wifi"|"ethernet", "active": true}]
    hosts = models.JSONField(default=list, blank=True)

    last_inform_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_task_error = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tr069'
        ordering = ['-last_inform_at', '-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['customer']),
        ]

    def __str__(self):
        return f"{self.label or self.serial_number} ({self.status})"

    @property
    def has_connected(self):
        return self.last_inform_at is not None

    @property
    def connected_hosts_count(self):
        return len(self.hosts or [])

    @property
    def wifi_network_count(self):
        return len(self.wifi_networks or [])


class CPETaskLog(models.Model):
    TASK_TYPES = (
        ('reboot', 'Reboot'),
        ('factory_reset', 'Factory Reset'),
        ('set_wifi', 'Change WiFi'),
        ('refresh', 'Ask for latest state'),
    )
    STATUS_CHOICES = (
        ('queued', 'Queued'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('expired', 'Expired'),
    )

    device = models.ForeignKey(CPEDevice, on_delete=models.CASCADE, related_name='task_logs')
    task_type = models.CharField(max_length=30, choices=TASK_TYPES)
    genieacs_task_id = models.CharField(max_length=255, blank=True, default='')
    params = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='queued')
    error_message = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(
        'core.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='tr069_tasks'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'tr069'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.task_type} -> {self.device.serial_number} ({self.status})"