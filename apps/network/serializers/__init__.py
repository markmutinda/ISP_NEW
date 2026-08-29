# apps/network/serializers/__init__.py
# apps/network/serializers/__init__.py

from .olt_serializers import *
from .tr069_serializers import *
from .router_serializers import *
from .ipam_serializers import *
from .access_point_serializers import AccessPointSerializer  # NEW

__all__ = [
    # OLT Serializers
    'OLTDeviceSerializer', 'OLTPortSerializer', 'PONPortSerializer',
    'ONUDeviceSerializer', 'OLTConfigSerializer',
    
    # TR-069 Serializers
    'ACSConfigurationSerializer', 'CPEDeviceSerializer',
    'TR069ParameterSerializer', 'TR069SessionSerializer',
    
    # Mikrotik Serializers
    'MikrotikDeviceSerializer', 'MikrotikInterfaceSerializer',
    'HotspotUserSerializer', 'PPPoEUserSerializer', 'MikrotikQueueSerializer',
    
    # IPAM Serializers
    'SubnetSerializer', 'VLANSerializer', 'IPPoolSerializer',
    'IPAddressSerializer', 'DHCPRangeSerializer',
    
    # Access Point Serializer (NEW)
    'AccessPointSerializer',
]