# apps/network/models/__init__.py

from .router_models import Router, RouterEvent  # NEW: Import Router and RouterEvent

from .olt_models import (
    OLTDevice, OLTPort, PONPort, ONUDevice, OLTConfig
)

from .tr069_models import (
    CPEDevice, TR069Parameter, TR069Session, ACSConfiguration
)

# IMPORTANT: These models still exist in their own file (e.g., mikrotik_models.py or router_submodels.py)
# If you renamed the file, change the import accordingly. For now, assuming the file still exists.
from .router_models import (
    MikrotikInterface,
    HotspotUser,
    PPPoEUser,
    MikrotikQueue,
)

from .ipam_models import (
    IPPool, IPAddress, DHCPRange, Subnet, VLAN
)

# Updated __all__ - Removed MikrotikDevice, added Router and RouterEvent
__all__ = [
    'Router', 'RouterEvent',                     # NEW
    'OLTDevice', 'OLTPort', 'PONPort', 'ONUDevice', 'OLTConfig',
    'CPEDevice', 'TR069Parameter', 'TR069Session', 'ACSConfiguration',
    'MikrotikInterface', 'HotspotUser', 'PPPoEUser', 'MikrotikQueue',
    'IPPool', 'IPAddress', 'DHCPRange', 'Subnet', 'VLAN'
]
