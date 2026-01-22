from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views.InvoiceViews import PlanViewSet, BillingCycleViewSet, InvoiceViewSet, InvoiceItemViewSet
from .views.PaymentViews import PaymentViewSet
from .views.VoucherViews import VoucherBatchViewSet, VoucherViewSet  # Removed VoucherUsageViewSet
from .views.hotspot_views import HotspotPlansView, HotspotPurchaseView, HotspotPurchaseStatusView
from .views.customer_payment_views import (
    InitiateCustomerPaymentView,
    CustomerPaymentStatusView,
    CustomerPaymentMethodsView,
)
from .views.webhook_views import (
    PayHeroSubscriptionWebhookView,
    PayHeroHotspotWebhookView,
    PayHeroBillingWebhookView,
)

router = DefaultRouter()

# Invoice URLs
router.register(r'plans', PlanViewSet, basename='plan')
router.register(r'billing-cycles', BillingCycleViewSet, basename='billing-cycle')
router.register(r'invoices', InvoiceViewSet, basename='invoice')
router.register(r'invoice-items', InvoiceItemViewSet, basename='invoice-item')

# Payment URLs
# router.register(r'payment-methods', PaymentMethodViewSet, basename='payment-method')  # Disabled
router.register(r'payments', PaymentViewSet, basename='payment')

# Voucher URLs
router.register(r'voucher-batches', VoucherBatchViewSet, basename='voucher-batch')
router.register(r'vouchers', VoucherViewSet, basename='voucher')
# Removed voucher-usages registration

urlpatterns = [
    path('', include(router.urls)),

    # Additional endpoints
    path('mpesa/callback/', PaymentViewSet.as_view({'post': 'mpesa_callback'}), name='mpesa-callback'),
    path('payments/payhero/callback/', PaymentViewSet.as_view({'post': 'payhero_callback'}), name='payhero-callback'),

    # Dashboard endpoints
    path('dashboard/invoice-stats/', InvoiceViewSet.as_view({'get': 'dashboard_stats'}), name='invoice-dashboard-stats'),
    path('dashboard/payment-stats/', PaymentViewSet.as_view({'get': 'dashboard_stats'}), name='payment-dashboard-stats'),

    # Customer endpoints
    path('customer/outstanding/', InvoiceViewSet.as_view({'get': 'customer_outstanding'}), name='customer-outstanding'),
    # Removed voucher-history endpoint

    # Utility endpoints
    path('vouchers/validate/', VoucherViewSet.as_view({'post': 'validate_code'}), name='voucher-validate'),
    
    # ─────────────────────────────────────────────────────────────
    # Customer Payment Initiation (payments to Netily → ISP)
    # ─────────────────────────────────────────────────────────────
    path('payments/initiate/', InitiateCustomerPaymentView.as_view(), name='initiate-payment'),
    path('payments/<int:payment_id>/status/', CustomerPaymentStatusView.as_view(), name='payment-status'),
    path('payment-methods/', CustomerPaymentMethodsView.as_view(), name='payment-methods'),
]

# ─────────────────────────────────────────────────────────────
# Hotspot URLs (PUBLIC - no auth)
# These are accessed from captive portal
# ─────────────────────────────────────────────────────────────
hotspot_urlpatterns = [
    path('routers/<int:router_id>/plans/', HotspotPlansView.as_view(), name='hotspot-plans'),
    path('purchase/', HotspotPurchaseView.as_view(), name='hotspot-purchase'),
    path('purchase/<str:session_id>/status/', HotspotPurchaseStatusView.as_view(), name='hotspot-status'),
]

# ─────────────────────────────────────────────────────────────
# PayHero Webhook URLs (PUBLIC - no auth)
# These receive callbacks from PayHero
# ─────────────────────────────────────────────────────────────
webhook_urlpatterns = [
    path('subscription/', PayHeroSubscriptionWebhookView.as_view(), name='payhero-subscription-webhook'),
    path('hotspot/', PayHeroHotspotWebhookView.as_view(), name='payhero-hotspot-webhook'),
    path('billing/', PayHeroBillingWebhookView.as_view(), name='payhero-billing-webhook'),
]

app_name = 'billing'

