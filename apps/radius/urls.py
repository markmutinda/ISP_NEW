"""
RADIUS URL Configuration
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    RadiusDashboardView,
    RadiusActiveSessionsView,
    RadiusUserView,
    RadiusUserActionView,
    RadAcctViewSet,
    NasViewSet,
    RadiusBandwidthProfileViewSet,
    RadPostAuthViewSet,
    RadiusSyncView,
)

router = DefaultRouter()
router.register(r'accounting', RadAcctViewSet, basename='radius-accounting')
router.register(r'nas', NasViewSet, basename='radius-nas')
router.register(r'profiles', RadiusBandwidthProfileViewSet, basename='radius-profile')
router.register(r'auth-logs', RadPostAuthViewSet, basename='radius-auth-log')

urlpatterns = [
    # Dashboard
    path('dashboard/', RadiusDashboardView.as_view(), name='radius-dashboard'),
    path('sessions/active/', RadiusActiveSessionsView.as_view(), name='radius-active-sessions'),
    
    # User management
    path('users/', RadiusUserView.as_view(), name='radius-user-list'),
    path('users/<str:username>/', RadiusUserView.as_view(), name='radius-user-detail'),
    path('users/<str:username>/<str:action>/', RadiusUserActionView.as_view(), name='radius-user-action'),
    
    # Sync endpoints
    path('sync/<str:sync_type>/', RadiusSyncView.as_view(), name='radius-sync'),
    
    # ViewSet routes
    path('', include(router.urls)),
]
