# apps/network/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

# ===== ROUTER / PUBLIC VIEWS =====
from apps.network.views.router_views import (
    RouterViewSet,
    RouterAuthenticateView,
    RouterHeartbeatView,
    RouterPortScanView,
    RouterPortsView,
    RouterHotspotConfigView,
    RouterHotspotConfigureView,
    RouterHotspotDisableView,
    RouterHotspotEnableView,
    RouterHotspotUpdateView,
    RouterBridgePortView,
    RouterHotspotIPAMView,
    RouterHotspotIPAMApplyView,
    RouterPortManagerView,
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

# ===== IP BINDING =====
from apps.network.views.ip_binding_views import IPBindingViewSet, RouterKnownHostsView

# ===== ACCESS POINTS (NEW) =====
from apps.network.views.access_point_views import AccessPointViewSet

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

# Register IP Binding ViewSet
router.register(r'ip-bindings', IPBindingViewSet, basename='ip-binding')

# Register Access Point ViewSet (NEW)
router.register(r'access-points', AccessPointViewSet, basename='access-point')

# =========================
# URLPATTERNS - Clean & Conflict-Free
# =========================
urlpatterns = [
    # Router Authentication (public endpoint)
    path('routers/auth/', RouterAuthenticateView.as_view(), name='router-auth'),
    path('routers/heartbeat/', RouterHeartbeatView.as_view(), name='router-heartbeat'),
    
    # Router Port Scan & Hotspot Configuration Endpoints
    path('routers/<int:pk>/scan/', RouterPortScanView.as_view(), name='router-port-scan'),
    path('routers/<int:pk>/ports/', RouterPortsView.as_view(), name='router-ports'),
    path('routers/<int:pk>/hotspot/config/', RouterHotspotConfigView.as_view(), name='router-hotspot-config'),
    path('routers/<int:pk>/hotspot/configure/', RouterHotspotConfigureView.as_view(), name='router-hotspot-configure'),
    
    # Router certificate download
    path('routers/<int:router_id>/cert/<str:cert_type>/', download_router_cert, name='router-cert-download'),
    
    path('routers/<int:pk>/hotspot/disable/', RouterHotspotDisableView.as_view(), name='router-hotspot-disable'),
    path('routers/<int:pk>/hotspot/enable/', RouterHotspotEnableView.as_view(), name='router-hotspot-enable'),
    path('routers/<int:pk>/hotspot/update/', RouterHotspotUpdateView.as_view(), name='router-hotspot-update'),
    path('routers/<int:pk>/bridge/port/', RouterBridgePortView.as_view(), name='router-bridge-port'),

    # Router Port Manager
    path('routers/<int:pk>/port-manager/', RouterPortManagerView.as_view(), name='router-port-manager'),
    
    # Hotspot IPAM (IP Address + Subnet Configuration)
    path('routers/<int:pk>/hotspot/ipam/', RouterHotspotIPAMView.as_view(), name='router-hotspot-ipam'),
    path('routers/<int:pk>/hotspot/ipam/apply/', RouterHotspotIPAMApplyView.as_view(), name='router-hotspot-ipam-apply'),

    # ────────────────────────────────────────────────────────────────
    # DIAGNOSTICS ENDPOINTS
    # ────────────────────────────────────────────────────────────────
    # DRF @action with slash in url_path needs an explicit route
    path('routers/<int:pk>/diagnose/', RouterViewSet.as_view({'get': 'diagnose'}), name='router-diagnose'),
    path('routers/<int:pk>/diagnose/fix/', RouterViewSet.as_view({'post': 'diagnose_fix'}), name='router-diagnose-fix'),

    # ─── Provisioning Endpoints (PUBLIC — for MikroTik /tool fetch) ───
    # Stage 1: Base script download (the "Magic Link" destination)
    path('provision/<str:auth_key>/<slug:slug>/script.rsc',
         ProvisionBaseScriptView.as_view(), name='provision-base-script'),
    
    # Stage 2: Version-specific config download
    path('provision/<str:auth_key>/<slug:slug>/config',
         ProvisionConfigView.as_view(), name='provision-config'),
    
    # Certificate downloads
    path('provision/<str:auth_key>/certs/<str:cert_type>',
         ProvisionCertView.as_view(), name='provision-cert'),
    
    # Hotspot HTML downloads
    path('provision/<str:auth_key>/hotspot/<str:page>',
         ProvisionHotspotHTMLView.as_view(), name='provision-hotspot-html'),
    
    # Legacy: Single-script download (backward compat)
    path('routers/config/', LegacyScriptDownloadView.as_view(), name='legacy-script-download'),
    
    # ─── IP Binding Endpoints ───
    # Router known hosts (DHCP + ARP) for IP binding picker
    path('routers/<int:router_id>/known-hosts/', RouterKnownHostsView.as_view(), name='router-known-hosts'),
    
    # DRF Router URLs
    path('', include(router.urls)),
]