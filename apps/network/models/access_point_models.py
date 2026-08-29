# apps/network/models/access_point_models.py
import uuid
from django.db import models
from django.utils import timezone
from apps.core.models import AuditMixin
from apps.network.models.router_models import Router


class AccessPoint(AuditMixin):
    """A physical AP/switch node downstream of a MikroTik's hotspot/PPPoE
    bridge. Online/offline is inferred from the bridge MAC-table + ARP —
    no agent needed on the AP itself."""

    STATUS_CHOICES = [
        ('online', 'Online'),
        ('offline', 'Offline'),
        ('unknown', 'Unknown'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    router = models.ForeignKey(Router, on_delete=models.CASCADE, related_name='access_points')
    parent = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='children',
        help_text='Upstream AP this node chains from (topology rendering only)'
    )

    name = models.CharField(max_length=100)
    mac_address = models.CharField(max_length=17, db_index=True)
    ip_address = models.GenericIPAddressField(protocol='IPv4', null=True, blank=True)

    # Canvas position for the frontend topology map
    pos_x = models.FloatField(default=0)
    pos_y = models.FloatField(default=0)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='unknown', db_index=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    last_checked = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [('router', 'mac_address')]
        ordering = ['name']
        indexes = [
            models.Index(fields=['router', 'status'], name='network_ap_router_status_idx'),
        ]

    def __str__(self):
        return f"{self.name} ({self.mac_address}) - {self.status}"