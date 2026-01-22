"""
Subscription URLs

Endpoints for Netily platform subscriptions and ISP payout management.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    NetilyPlanViewSet,
    CurrentSubscriptionView,
    SubscriptionUsageView,
    InitiateSubscriptionPaymentView,
    SubscriptionPaymentViewSet,
    CancelSubscriptionView,
    ISPPayoutConfigView,
    VerifyPayoutView,
    SettlementSummaryView,
    SettlementHistoryViewSet,
)

app_name = 'subscriptions'

router = DefaultRouter()
router.register(r'plans', NetilyPlanViewSet, basename='plans')
router.register(r'payments', SubscriptionPaymentViewSet, basename='payments')

urlpatterns = [
    # Include router URLs
    path('', include(router.urls)),
    
    # Subscription management
    path('current/', CurrentSubscriptionView.as_view(), name='current'),
    path('usage/', SubscriptionUsageView.as_view(), name='usage'),
    path('pay/', InitiateSubscriptionPaymentView.as_view(), name='pay'),
    path('cancel/', CancelSubscriptionView.as_view(), name='cancel'),
]

# Settlement/Payout URLs (these go under /api/v1/core/)
settlement_router = DefaultRouter()
settlement_router.register(r'settlements', SettlementHistoryViewSet, basename='settlements')

payout_urlpatterns = [
    path('payout-config/', ISPPayoutConfigView.as_view(), name='payout-config'),
    path('payout-config/verify/', VerifyPayoutView.as_view(), name='payout-verify'),
    path('settlements/summary/', SettlementSummaryView.as_view(), name='settlement-summary'),
    path('', include(settlement_router.urls)),
]
