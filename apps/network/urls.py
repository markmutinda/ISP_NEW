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

# ===== PROVISIONING (Public — for MikroTik /tool fetch) =====
from apps.network.views.provision_views import (
    ProvisionBaseScriptView,
    ProvisionConfigView,
    ProvisionCertView,
    ProvisionHotspotHTMLView,
    LegacyScriptDownloadView,
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

<<<<<<< HEAD
    # ─── Provisioning Endpoints (PUBLIC — for MikroTik /tool fetch) ───
    # Stage 1: Base script download (the "Magic Link" destination)
    path('network/provision/<str:auth_key>/<slug:slug>/script.rsc',
         ProvisionBaseScriptView.as_view(), name='provision-base-script'),
    
    # Stage 2: Version-specific config download
    path('network/provision/<str:auth_key>/config',
         ProvisionConfigView.as_view(), name='provision-config'),
    
    # Certificate downloads
    path('network/provision/<str:auth_key>/certs/<str:cert_type>',
         ProvisionCertView.as_view(), name='provision-cert'),
    
    # Hotspot HTML downloads
    path('network/provision/<str:auth_key>/hotspot/<str:page>',
         ProvisionHotspotHTMLView.as_view(), name='provision-hotspot-html'),
    
    # Legacy: Single-script download (backward compat)
    path('network/routers/config/', LegacyScriptDownloadView.as_view(), name='legacy-script-download'),

    
=======
>>>>>>> 9fb26f9b9e1561c3cadb44471a2dfdfa8d44d90a
    path('', include(router.urls)),
]

