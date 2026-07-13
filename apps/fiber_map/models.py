"""
apps/fiber_map/models.py
Per-tenant fiber/network infrastructure mapping: cables, splitters, ODFs,
poles, drops, and fault/cut markers plotted on a map.
"""
import uuid
from django.conf import settings
from django.db import models


class NetworkMapElement(models.Model):
    ELEMENT_TYPES = (
        ('CABLE', 'Fiber Cable'),
        ('SPLITTER', 'Splitter'),
        ('ODF', 'ODF / Distribution Frame'),
        ('NAP', 'Network Access Point'),
        ('POLE', 'Pole'),
        ('MANHOLE', 'Manhole / Handhole'),
        ('JOINT_CLOSURE', 'Joint Closure'),
        ('CUSTOMER_DROP', 'Customer Drop'),
        ('EQUIPMENT', 'Router / Equipment'),
        ('ISSUE', 'Fault / Cut'),
        ('OTHER', 'Other'),
    )

    GEOMETRY_TYPES = (
        ('POINT', 'Point'),
        ('LINE', 'Line'),
    )

    STATUS_CHOICES = (
        ('ACTIVE', 'Active'),
        ('PLANNED', 'Planned'),
        ('FAULTY', 'Faulty'),
        ('DECOMMISSIONED', 'Decommissioned'),
    )

    SEVERITY_CHOICES = (
        ('', '—'),
        ('LOW', 'Low'),
        ('MEDIUM', 'Medium'),
        ('HIGH', 'High'),
        ('CRITICAL', 'Critical'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    name = models.CharField(max_length=255)
    element_type = models.CharField(max_length=20, choices=ELEMENT_TYPES, default='OTHER', db_index=True)
    geometry_type = models.CharField(max_length=10, choices=GEOMETRY_TYPES, default='POINT')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE', db_index=True)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, blank=True, default='')

    # [[lat, lng], ...] — one pair for POINT, 2+ pairs for LINE
    coordinates = models.JSONField(default=list)

    # Free-form attributes: core_count, cable_type, split_ratio, etc.
    properties = models.JSONField(default=dict, blank=True)

    color = models.CharField(max_length=20, blank=True, default='')
    notes = models.TextField(blank=True, default='')

    parent = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='children'
    )
    # Informational link only — no FK constraint, so this app never depends
    # on the exact shape of apps.network.Router.
    linked_router_id = models.PositiveIntegerField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_map_elements',
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='updated_map_elements',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'fiber_map'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['element_type', 'status']),
        ]

    def __str__(self):
        return f"{self.get_element_type_display()}: {self.name}"