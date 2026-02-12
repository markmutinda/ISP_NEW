# apps/network/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

# ===== ROUTER / PUBLIC VIEWS =====
from apps.network.views.router_views import (
    RouterViewSet,
    RouterAuthenticateView,
    RouterHeartbeatView,
    RouterPortsView,
    RouterHotspotConfigView,
    RouterHotspotConfigureView,
    RouterHotspotDisableView,
    download_router_cert,
)

# ===== IPAM =====
from apps.network.views.ipam_views import (
    SubnetViewSet,
    VLANViewSet,
    IPPoolViewSet,
    IPAddressViewSet,
    DHCPRangeViewSet,
)

# ===== OLT =====
from apps.network.views.olt_views import (
    OLTDeviceViewSet,
    OLTPortViewSet,
    PONPortViewSet,
    ONUDeviceViewSet,
    OLTConfigViewSet,
)

# ===== TR-069 =====
from apps.network.views.tr069_views import (
    ACSConfigurationViewSet,
    CPEDeviceViewSet,
    TR069ParameterViewSet,
    TR069SessionViewSet,
)

# =========================
# DRF ROUTER
# =========================
router = DefaultRouter()

# Register ALL viewsets - no basename conflicts
router.register(r'routers', RouterViewSet)
router.register(r'subnets', SubnetViewSet)
router.register(r'vlans', VLANViewSet)
router.register(r'ip-pools', IPPoolViewSet)
router.register(r'ip-addresses', IPAddressViewSet)
router.register(r'dhcp-ranges', DHCPRangeViewSet)
router.register(r'olts', OLTDeviceViewSet)
router.register(r'olt-ports', OLTPortViewSet)
router.register(r'pon-ports', PONPortViewSet)
router.register(r'onus', ONUDeviceViewSet)
router.register(r'olt-configs', OLTConfigViewSet)
router.register(r'acs-configs', ACSConfigurationViewSet)
router.register(r'cpe-devices', CPEDeviceViewSet)
router.register(r'tr069-parameters', TR069ParameterViewSet)
router.register(r'tr069-sessions', TR069SessionViewSet)

# =========================
# URLPATTERNS - Clean & Conflict-Free
# =========================
urlpatterns = [
    path('network/routers/auth/', RouterAuthenticateView.as_view(), name='router-auth'),
    path('routers/heartbeat/', RouterHeartbeatView.as_view(), name='router-heartbeat'),
    
    # Router Hotspot Configuration Endpoints
    path('routers/<int:pk>/ports/', RouterPortsView.as_view(), name='router-ports'),
    path('routers/<int:pk>/hotspot/config/', RouterHotspotConfigView.as_view(), name='router-hotspot-config'),
    path('routers/<int:pk>/hotspot/configure/', RouterHotspotConfigureView.as_view(), name='router-hotspot-configure'),
    
    # This line works now because we imported the function above
    path('routers/<int:router_id>/cert/<str:cert_type>/', download_router_cert, name='router-cert-download'),
    
    path('routers/<int:pk>/hotspot/disable/', RouterHotspotDisableView.as_view(), name='router-hotspot-disable'),

    path('', include(router.urls)),
]

