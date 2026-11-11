# apps/network/models/load_balance_models.py
"""
Multi-WAN load balancing configuration for MikroTik routers.

Kept as a separate OneToOne model (not fields on Router) on purpose:
saving through Router.save() triggers handle_router_lifecycle, which
recreates the NAS entry and restarts FreeRADIUS. Config changes to load
balancing must not touch RADIUS state.
"""

from django.db import models

from apps.core.models import AuditMixin
from .router_models import Router


class RouterLoadBalance(AuditMixin):
    """
    Load-balancing configuration for a single router.

    - uplinks: list of dicts, each describing one ISP (see
      apps.network.services.load_balancer.validate_uplinks for schema).
    - applied_hash: SHA1 of the last script the router acknowledged via
      the lb-ack endpoint. Empty until first successful import.
    - last_applied_at: timestamp of that acknowledgement.
    """

    router = models.OneToOneField(
        Router,
        on_delete=models.CASCADE,
        related_name='load_balance',
    )
    is_enabled = models.BooleanField(default=False)
    uplinks = models.JSONField(default=list, blank=True)
    applied_hash = models.CharField(max_length=40, blank=True, default='')
    last_applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Router Load Balance'
        verbose_name_plural = 'Router Load Balances'

    def __str__(self):
        state = 'enabled' if self.is_enabled else 'disabled'
        return f"{self.router.name} ({state}, {len(self.uplinks or [])} uplinks)"