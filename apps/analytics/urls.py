from django.urls import path
from .views_v1 import AnalyticsDashboardView
from .individual_views import (
    AnalyticsKPIsView,
    AnalyticsRevenueView,
    AnalyticsUserGrowthView,
    AnalyticsPlanPerformanceView,
    AnalyticsLocationsView,
    AnalyticsRoutersView,
    AnalyticsPaymentMethodsView,
    AnalyticsPaymentStatsView,
    AnalyticsUserDistributionView,
    AnalyticsRevenueByTypeView,
    AnalyticsRevenueForecastView,
    AnalyticsRevenueTargetView,
    AnalyticsNetworkStatsView,
    AnalyticsExportView,
)

app_name = 'analytics'

urlpatterns = [
    # Main dashboard endpoint (recommended)
    path('dashboard/', AnalyticsDashboardView.as_view(), name='analytics-dashboard'),
    
    # Individual endpoints
    path('kpis/', AnalyticsKPIsView.as_view(), name='analytics-kpis'),
    path('revenue/', AnalyticsRevenueView.as_view(), name='analytics-revenue'),
    path('user-growth/', AnalyticsUserGrowthView.as_view(), name='analytics-user-growth'),
    path('plans/', AnalyticsPlanPerformanceView.as_view(), name='analytics-plans'),
    path('locations/', AnalyticsLocationsView.as_view(), name='analytics-locations'),
    path('routers/', AnalyticsRoutersView.as_view(), name='analytics-routers'),
    path('payment-methods/', AnalyticsPaymentMethodsView.as_view(), name='analytics-payment-methods'),
    path('payment-stats/', AnalyticsPaymentStatsView.as_view(), name='analytics-payment-stats'),
    path('user-distribution/', AnalyticsUserDistributionView.as_view(), name='analytics-user-distribution'),
    path('revenue-by-type/', AnalyticsRevenueByTypeView.as_view(), name='analytics-revenue-by-type'),
    path('revenue-forecast/', AnalyticsRevenueForecastView.as_view(), name='analytics-revenue-forecast'),
    path('revenue-target/', AnalyticsRevenueTargetView.as_view(), name='analytics-revenue-target'),
    path('network-stats/', AnalyticsNetworkStatsView.as_view(), name='analytics-network-stats'),
    path('export/', AnalyticsExportView.as_view(), name='analytics-export'),
]