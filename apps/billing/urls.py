from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views.InvoiceViews import PlanViewSet, BillingCycleViewSet, InvoiceViewSet, InvoiceItemViewSet
from .views.PaymentViews import PaymentViewSet
from .views.VoucherViews import VoucherBatchViewSet, VoucherViewSet  # Removed VoucherUsageViewSet

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
]

app_name = 'billing'

