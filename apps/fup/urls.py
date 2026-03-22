from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    FUPDashboardSummaryView,
    FUPPolicyViewSet,
    FUPViolationViewSet,
    FUPThrottleStateViewSet,
    FUPAnalyticsOverviewView,
)

router = DefaultRouter()
router.register(r'policies', FUPPolicyViewSet, basename='fup-policy')
router.register(r'violations', FUPViolationViewSet, basename='fup-violation')
router.register(r'throttled', FUPThrottleStateViewSet, basename='fup-throttled')

urlpatterns = [
    path('dashboard/summary/', FUPDashboardSummaryView.as_view(), name='fup-dashboard-summary'),
    path('analytics/overview/', FUPAnalyticsOverviewView.as_view(), name='fup-analytics-overview'),
    path('', include(router.urls)),
]