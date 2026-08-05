import uuid
from django.db import models
from django.utils import timezone


class IPBinding(models.Model):
    """
    Manual MAC/IP binding for devices that can't use the captive portal
    (Smart TVs, consoles, IoT). Bypasses RADIUS entirely — uses MikroTik's
    native /ip hotspot ip-binding (bypassed) + a Simple Queue for speed control.
    """
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('disabled', 'Disabled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    router = models.ForeignKey('network.Router', on_delete=models.CASCADE, related_name='ip_bindings')
    plan = models.ForeignKey('billing.HotspotPlan', on_delete=models.SET_NULL, null=True, related_name='ip_bindings')

    name = models.CharField(max_length=100, help_text="Friendly label, e.g. 'Living Room TV'")
    mac_address = models.CharField(max_length=17, db_index=True)
    ip_address = models.GenericIPAddressField(protocol='IPv4', null=True, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active', db_index=True)
    activated_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    mikrotik_binding_id = models.CharField(max_length=32, blank=True)
    queue_name = models.CharField(max_length=64, blank=True)

    notes = models.TextField(blank=True)
    created_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='created_ip_bindings')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = [('router', 'mac_address')]
        indexes = [
            models.Index(fields=['router', 'status']),
            models.Index(fields=['status', 'expires_at']),
        ]

    def __str__(self):
        return f"{self.name} ({self.mac_address}) - {self.status}"

    @property
    def is_active(self) -> bool:
        if self.status != 'active':
            return False
        return not (self.expires_at and self.expires_at < timezone.now())

    @property
    def time_remaining_minutes(self) -> int:
        if not self.expires_at:
            return 0
        return max(0, int((self.expires_at - timezone.now()).total_seconds() / 60))