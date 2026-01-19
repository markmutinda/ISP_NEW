# apps/network/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

# ===== ROUTER / PUBLIC VIEWS =====
from apps.network.views.router_views import (
    RouterViewSet,
    RouterAuthenticateView,
    RouterHeartbeatView,
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
    # 1. All protected ViewSet routes (list + detail + actions)
    path('', include(router.urls)),

    # 2. Public router endpoints (specific paths - placed AFTER router to avoid shadowing)
    path('routers/auth/', RouterAuthenticateView.as_view(), name='router-authenticate'),
    path('routers/heartbeat/', RouterHeartbeatView.as_view(), name='router-heartbeat'),
]

