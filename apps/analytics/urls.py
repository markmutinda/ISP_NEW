from django.urls import path
from .views_v1 import AnalyticsDashboardView
from .views_reports import ReportsDataView
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
from .frontend_contract_views import (
    AnalyticsReportsView,
    AnalyticsChurnView,
    AnalyticsCustomersView,
    AnalyticsRevenueView as AnalyticsRevenueContractView,
    AnalyticsUsageView,
)

app_name = 'analytics'

urlpatterns = [
    # Main dashboard endpoint (recommended)
    path('dashboard/', AnalyticsDashboardView.as_view(), name='analytics-dashboard'),
    
    # Reports & Analytics (4-tab page)
    path('reports/', ReportsDataView.as_view(), name='analytics-reports'),
    
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
    
    # ============================================================
    # NEW: Frontend Contract Endpoints
    # These endpoints follow the exact contract expected by the frontend
    # ============================================================
    path('reports-contract/', AnalyticsReportsView.as_view(), name='analytics-reports-contract'),
    path('churn/', AnalyticsChurnView.as_view(), name='analytics-churn'),
    path('customers/', AnalyticsCustomersView.as_view(), name='analytics-customers'),
    path('revenue-contract/', AnalyticsRevenueContractView.as_view(), name='analytics-revenue-contract'),
    path('usage/', AnalyticsUsageView.as_view(), name='analytics-usage'),
]