"""
VPN URL Configuration
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    VPNDashboardView,
    VPNActiveConnectionsView,
    CertificateAuthorityViewSet,
    VPNCertificateViewSet,
    VPNServerViewSet,
    VPNConnectionViewSet,
    VPNConnectionLogViewSet,
    RouterVPNStatusView,
    GenerateRouterCertificateView,
)

router = DefaultRouter()
router.register(r'cas', CertificateAuthorityViewSet, basename='certificate-authority')
router.register(r'certificates', VPNCertificateViewSet, basename='vpn-certificate')
router.register(r'servers', VPNServerViewSet, basename='vpn-server')
router.register(r'connections', VPNConnectionViewSet, basename='vpn-connection')
router.register(r'logs', VPNConnectionLogViewSet, basename='vpn-log')

urlpatterns = [
    # Dashboard
    path('dashboard/', VPNDashboardView.as_view(), name='vpn-dashboard'),
    path('connections/active/', VPNActiveConnectionsView.as_view(), name='vpn-active-connections'),
    
    # Router-specific endpoints
    path('routers/<uuid:router_id>/status/', RouterVPNStatusView.as_view(), name='router-vpn-status'),
    path('routers/<uuid:router_id>/generate-certificate/', GenerateRouterCertificateView.as_view(), name='router-generate-certificate'),
    
    # ViewSet routes
    path('', include(router.urls)),
]
