# apps/network/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.network.views import (
    # OLT Views
    OLTDeviceViewSet, OLTPortViewSet, PONPortViewSet,
    ONUDeviceViewSet, OLTConfigViewSet,
    
    # TR-069 Views
    ACSConfigurationViewSet, CPEDeviceViewSet,
    TR069ParameterViewSet, TR069SessionViewSet,
    
    # Mikrotik Views
    MikrotikDeviceViewSet, MikrotikInterfaceViewSet,
    HotspotUserViewSet, PPPoEUserViewSet, MikrotikQueueViewSet,
    
    # IPAM Views
    SubnetViewSet, VLANViewSet, IPPoolViewSet,
    IPAddressViewSet, DHCPRangeViewSet,
)

router = DefaultRouter()

# OLT Routes
router.register(r'olts', OLTDeviceViewSet, basename='olt')
router.register(r'olt-ports', OLTPortViewSet, basename='olt-port')
router.register(r'pon-ports', PONPortViewSet, basename='pon-port')
router.register(r'onus', ONUDeviceViewSet, basename='onu')
router.register(r'olt-configs', OLTConfigViewSet, basename='olt-config')

# TR-069 Routes
router.register(r'acs-configs', ACSConfigurationViewSet, basename='acs-config')
router.register(r'cpe-devices', CPEDeviceViewSet, basename='cpe-device')
router.register(r'tr069-parameters', TR069ParameterViewSet, basename='tr069-parameter')
router.register(r'tr069-sessions', TR069SessionViewSet, basename='tr069-session')

# Mikrotik Routes
router.register(r'mikrotik-devices', MikrotikDeviceViewSet, basename='mikrotik-device')
router.register(r'mikrotik-interfaces', MikrotikInterfaceViewSet, basename='mikrotik-interface')
router.register(r'hotspot-users', HotspotUserViewSet, basename='hotspot-user')
router.register(r'pppoe-users', PPPoEUserViewSet, basename='pppoe-user')
router.register(r'mikrotik-queues', MikrotikQueueViewSet, basename='mikrotik-queue')

# IPAM Routes
router.register(r'subnets', SubnetViewSet, basename='subnet')
router.register(r'vlans', VLANViewSet, basename='vlan')
router.register(r'ip-pools', IPPoolViewSet, basename='ip-pool')
router.register(r'ip-addresses', IPAddressViewSet, basename='ip-address')
router.register(r'dhcp-ranges', DHCPRangeViewSet, basename='dhcp-range')

urlpatterns = [
    path('', include(router.urls)),
]