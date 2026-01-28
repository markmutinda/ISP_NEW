# apps/network/views/__init__.py

from .olt_views import *
from .tr069_views import *
from .router_views import *
from .ipam_views import *

__all__ = [
    # Router Management Views (NEW - replaces Mikrotik)
    'RouterViewSet',
    'RouterAuthenticateView',
    'RouterHeartbeatView',
    'MikrotikInterfaceViewSet',      # Still exists (linked to Router)
    'HotspotUserViewSet',            # Still exists
    'PPPoEUserViewSet',              # Still exists
    'MikrotikQueueViewSet',          # Still exists

    # OLT Views
    'OLTDeviceViewSet',
    'OLTPortViewSet',
    'PONPortViewSet',
    'ONUDeviceViewSet',
    'OLTConfigViewSet',

    # TR-069 Views
    'ACSConfigurationViewSet',
    'CPEDeviceViewSet',
    'TR069ParameterViewSet',
    'TR069SessionViewSet',

    # IPAM Views
    'SubnetViewSet',
    'VLANViewSet',
    'IPPoolViewSet',
    'IPAddressViewSet',
    'DHCPRangeViewSet',
    
    # Router Hotspot Configuration
    'RouterPortsView',
    'RouterHotspotConfigView',
    'RouterHotspotConfigureView',
    'RouterHotspotDisableView',
]

