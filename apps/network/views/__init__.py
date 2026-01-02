# apps/network/views/__init__.py
from .olt_views import *
from .tr069_views import *
from .mikrotik_views import *
from .ipam_views import *

__all__ = [
    # OLT Views
    'OLTDeviceViewSet', 'OLTPortViewSet', 'PONPortViewSet',
    'ONUDeviceViewSet', 'OLTConfigViewSet',
    
    # TR-069 Views
    'ACSConfigurationViewSet', 'CPEDeviceViewSet',
    'TR069ParameterViewSet', 'TR069SessionViewSet',
    
    # Mikrotik Views
    'MikrotikDeviceViewSet', 'MikrotikInterfaceViewSet',
    'HotspotUserViewSet', 'PPPoEUserViewSet', 'MikrotikQueueViewSet',
    
    # IPAM Views
    'SubnetViewSet', 'VLANViewSet', 'IPPoolViewSet',
    'IPAddressViewSet', 'DHCPRangeViewSet',
]
