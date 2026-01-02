from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    BandwidthProfileViewSet,
    TrafficRuleViewSet,
    DataUsageViewSet,
    BandwidthAlertViewSet,
    SpeedTestResultViewSet,
    TrafficAnalysisViewSet
)

router = DefaultRouter()
router.register(r'profiles', BandwidthProfileViewSet, basename='bandwidth-profile')
router.register(r'traffic-rules', TrafficRuleViewSet, basename='traffic-rule')
router.register(r'usage', DataUsageViewSet, basename='data-usage')
router.register(r'alerts', BandwidthAlertViewSet, basename='bandwidth-alert')
router.register(r'speed-tests', SpeedTestResultViewSet, basename='speed-test')
router.register(r'analysis', TrafficAnalysisViewSet, basename='traffic-analysis')

urlpatterns = [
    path('', include(router.urls)),
]