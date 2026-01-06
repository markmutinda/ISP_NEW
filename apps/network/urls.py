# apps/network/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.network.views.router_views import (
    RouterViewSet,
    RouterAuthenticateView,
    RouterHeartbeatView,
)

# Import other viewsets (keep existing ones)
from apps.network.views.ipam_views import (
    SubnetViewSet, VLANViewSet, IPPoolViewSet,
    IPAddressViewSet, DHCPRangeViewSet,
)
from apps.network.views.olt_views import (
    OLTDeviceViewSet, OLTPortViewSet, PONPortViewSet,
    ONUDeviceViewSet, OLTConfigViewSet,
)
from apps.network.views.tr069_views import (
    ACSConfigurationViewSet, CPEDeviceViewSet,
    TR069ParameterViewSet, TR069SessionViewSet,
)
# If you still have granular Mikrotik views, import them too
# from apps.network.views.router_views import (
#     MikrotikInterfaceViewSet, HotspotUserViewSet,
#     PPPoEUserViewSet, MikrotikQueueViewSet,
# )

router = DefaultRouter()

# === NEW: Router Management ===
router.register(r'routers', RouterViewSet, basename='router')

# === IPAM ===
router.register(r'subnets', SubnetViewSet, basename='subnet')
router.register(r'vlans', VLANViewSet, basename='vlan')
router.register(r'ip-pools', IPPoolViewSet, basename='ip-pool')
router.register(r'ip-addresses', IPAddressViewSet, basename='ip-address')
router.register(r'dhcp-ranges', DHCPRangeViewSet, basename='dhcp-range')

# === OLT ===
router.register(r'olts', OLTDeviceViewSet, basename='olt')
router.register(r'olt-ports', OLTPortViewSet, basename='olt-port')
router.register(r'pon-ports', PONPortViewSet, basename='pon-port')
router.register(r'onus', ONUDeviceViewSet, basename='onu')
router.register(r'olt-configs', OLTConfigViewSet, basename='olt-config')

# === TR-069 ===
router.register(r'acs-configs', ACSConfigurationViewSet, basename='acs-config')
router.register(r'cpe-devices', CPEDeviceViewSet, basename='cpe-device')
router.register(r'tr069-parameters', TR069ParameterViewSet, basename='tr069-parameter')
router.register(r'tr069-sessions', TR069SessionViewSet, basename='tr069-session')

# === Optional: Granular Mikrotik sub-resources (if still needed) ===
# router.register(r'mikrotik-interfaces', MikrotikInterfaceViewSet, basename='mikrotik-interface')
# router.register(r'hotspot-users', HotspotUserViewSet, basename='hotspot-user')
# router.register(r'pppoe-users', PPPoEUserViewSet, basename='pppoe-user')
# router.register(r'mikrotik-queues', MikrotikQueueViewSet, basename='mikrotik-queue')

urlpatterns = [
    # Main API routes
    path('', include(router.urls)),

    # === PUBLIC ENDPOINTS - NO AUTHENTICATION REQUIRED ===
    path('routers/auth/', RouterAuthenticateView.as_view(), name='router-authenticate'),
    path('routers/heartbeat/', RouterHeartbeatView.as_view(), name='router-heartbeat'),
]