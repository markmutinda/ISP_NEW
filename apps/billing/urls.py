from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views.InvoiceViews import PlanViewSet, BillingCycleViewSet, InvoiceViewSet, InvoiceItemViewSet
from .views.PaymentViews import PaymentViewSet, MpesaConfigurationViewSet, MpesaTransactionViewSet
from .views.VoucherViews import VoucherBatchViewSet, VoucherViewSet

from .views.hotspot_views import CaptivePortalView, HotspotPlansView, HotspotPurchaseView, HotspotPurchaseStatusView

from .views.cloud_portal_views import (
    HotspotLoginPageView,
    HotspotAutoLoginView,
    HotspotReturnTripView,
    HotspotDeviceAuthView,
    HotspotDeviceAuthStatusView,
)
from .views.hotspot_admin_views import (
    HotspotPlanViewSet,
    HotspotSessionViewSet,
    HotspotBrandingView,
    HotspotDashboardView,
)
from .views.customer_payment_views import (
    InitiateCustomerPaymentView,
    CustomerPaymentStatusView,
    CustomerPaymentMethodsView,
)
from .views.webhook_views import (
    PayHeroSubscriptionWebhookView,
    PayHeroHotspotWebhookView,
    PayHeroBillingWebhookView,
    MpesaC2BWebhookView,  # ADDED: Import the new M-Pesa C2B webhook view
)

router = DefaultRouter()

# Invoice URLs
router.register(r'plans', PlanViewSet, basename='plan')
router.register(r'billing-cycles', BillingCycleViewSet, basename='billing-cycle')
router.register(r'invoices', InvoiceViewSet, basename='invoice')
router.register(r'invoice-items', InvoiceItemViewSet, basename='invoice-item')

# Payment URLs
router.register(r'payments', PaymentViewSet, basename='payment')

# M-Pesa Configuration URLs (NEW)
router.register(r'mpesa-config', MpesaConfigurationViewSet, basename='mpesa-config')
router.register(r'mpesa-transactions', MpesaTransactionViewSet, basename='mpesa-transaction')

# Voucher URLs
router.register(r'voucher-batches', VoucherBatchViewSet, basename='voucher-batch')
router.register(r'vouchers', VoucherViewSet, basename='voucher')

urlpatterns = [
    path('', include(router.urls)),

    # ==========================
    # M-Pesa Endpoints
    # ==========================
    # Main callback endpoint for Safaricom M-Pesa API
    # UPDATED: Changed from 'mpesa/callback/' to 'mpesa/c2b-callback/' to match Safaricom expectations
    path('mpesa/c2b-callback/', MpesaC2BWebhookView.as_view(), name='mpesa-c2b-callback'),
    
    # Keep the old callback for backward compatibility if needed
    path('mpesa/callback/', PaymentViewSet.as_view({'post': 'mpesa_callback'}), name='mpesa-callback-legacy'),
    
    # M-Pesa configuration test endpoint
    path('mpesa-config/<int:pk>/test/', 
         MpesaConfigurationViewSet.as_view({'post': 'test_connection'}), 
         name='mpesa-config-test'),
    
    # M-Pesa configuration management endpoints
    path('mpesa-config/<int:pk>/set-default/', 
         MpesaConfigurationViewSet.as_view({'post': 'set_default'}), 
         name='mpesa-config-set-default'),
    path('mpesa-config/<int:pk>/toggle-active/', 
         MpesaConfigurationViewSet.as_view({'post': 'toggle_active'}), 
         name='mpesa-config-toggle-active'),
    path('mpesa-config/active/', 
         MpesaConfigurationViewSet.as_view({'get': 'active'}), 
         name='mpesa-config-active'),
    path('mpesa-config/default/', 
         MpesaConfigurationViewSet.as_view({'get': 'default'}), 
         name='mpesa-config-default'),
    
    # M-Pesa transaction endpoints
    path('mpesa-transactions/<int:pk>/status/', 
         MpesaTransactionViewSet.as_view({'get': 'status'}), 
         name='mpesa-transaction-status'),

    # ==========================
    # PayHero Endpoints
    # ==========================
    path('payments/payhero/callback/', PaymentViewSet.as_view({'post': 'payhero_callback'}), name='payhero-callback'),

    # ==========================
    # Dashboard Endpoints
    # ==========================
    path('dashboard/invoice-stats/', InvoiceViewSet.as_view({'get': 'dashboard_stats'}), name='invoice-dashboard-stats'),
    path('dashboard/payment-stats/', PaymentViewSet.as_view({'get': 'dashboard_stats'}), name='payment-dashboard-stats'),

    # ==========================
    # Customer Endpoints
    # ==========================
    path('customer/outstanding/', InvoiceViewSet.as_view({'get': 'customer_outstanding'}), name='customer-outstanding'),

    # ==========================
    # Utility Endpoints
    # ==========================
    path('vouchers/validate/', VoucherViewSet.as_view({'post': 'validate_code'}), name='voucher-validate'),
    
    # ==========================
    # Customer Payment Initiation (payments to Netily → ISP)
    # ==========================
    path('payments/initiate/', InitiateCustomerPaymentView.as_view(), name='initiate-payment'),
    path('payments/<int:payment_id>/status/', CustomerPaymentStatusView.as_view(), name='payment-status'),
    path('payment-methods/', CustomerPaymentMethodsView.as_view(), name='payment-methods'),
]

# ==========================
# Hotspot URLs (PUBLIC - no auth)
# These are accessed from captive portal
# ==========================
hotspot_urlpatterns = [
    path('captive-portal/', CaptivePortalView.as_view(), name='hotspot-captive-portal'),
    path('routers/<int:router_id>/plans/', HotspotPlansView.as_view(), name='hotspot-plans'),
    path('purchase/', HotspotPurchaseView.as_view(), name='hotspot-purchase'),
    path('purchase/<str:session_id>/status/', HotspotPurchaseStatusView.as_view(), name='hotspot-status'),
    
    # ── Cloud Controller Portal Endpoints ──
    path('login-page/<int:router_id>/', HotspotLoginPageView.as_view(), name='hotspot-login-page'),
    path('auto-login/', HotspotAutoLoginView.as_view(), name='hotspot-auto-login'),
    path('return-trip/<str:session_id>/', HotspotReturnTripView.as_view(), name='hotspot-return-trip'),
    path('device-auth/request/', HotspotDeviceAuthView.as_view(), name='hotspot-device-auth-request'),
    path('device-auth/authorize/', HotspotDeviceAuthView.as_view(), name='hotspot-device-auth-authorize'),
    path('device-auth/status/', HotspotDeviceAuthStatusView.as_view(), name='hotspot-device-auth-status'),
]

# ==========================
# Hotspot Admin URLs (AUTHENTICATED - admin/staff only)
# These are used by the hotspot management admin page
# ==========================
hotspot_admin_urlpatterns = [
    # Dashboard
    path('dashboard/', HotspotDashboardView.as_view(), name='hotspot-dashboard'),
    
    # Plans CRUD (per-router) — prefixed with "admin/" to avoid collision with public plans endpoint
    path('admin/routers/<int:router_id>/plans/', 
         HotspotPlanViewSet.as_view({'get': 'list', 'post': 'create'}), 
         name='hotspot-admin-plans'),
    path('admin/routers/<int:router_id>/plans/<uuid:pk>/', 
         HotspotPlanViewSet.as_view({'get': 'retrieve', 'patch': 'partial_update', 'delete': 'destroy'}), 
         name='hotspot-admin-plan-detail'),
    path('admin/routers/<int:router_id>/plans/reorder/', 
         HotspotPlanViewSet.as_view({'post': 'reorder'}), 
         name='hotspot-admin-plans-reorder'),
    path('admin/routers/<int:router_id>/plans/<uuid:pk>/toggle-active/', 
         HotspotPlanViewSet.as_view({'post': 'toggle_active'}), 
         name='hotspot-admin-plan-toggle'),
    
    # Sessions (per-router, read-only with disconnect)
    path('admin/routers/<int:router_id>/sessions/', 
         HotspotSessionViewSet.as_view({'get': 'list'}), 
         name='hotspot-admin-sessions'),
    path('admin/routers/<int:router_id>/sessions/stats/', 
         HotspotSessionViewSet.as_view({'get': 'stats'}), 
         name='hotspot-admin-sessions-stats'),
    path('admin/routers/<int:router_id>/sessions/<uuid:pk>/', 
         HotspotSessionViewSet.as_view({'get': 'retrieve'}), 
         name='hotspot-admin-session-detail'),
    path('admin/routers/<int:router_id>/sessions/<uuid:pk>/disconnect/', 
         HotspotSessionViewSet.as_view({'post': 'disconnect'}), 
         name='hotspot-admin-session-disconnect'),
    
    # Branding (per-router)
    path('admin/routers/<int:router_id>/branding/', 
         HotspotBrandingView.as_view(), 
         name='hotspot-admin-branding'),
]

# ==========================
# PayHero Webhook URLs (PUBLIC - no auth)
# These receive callbacks from PayHero
# ==========================
webhook_urlpatterns = [
    path('subscription/', PayHeroSubscriptionWebhookView.as_view(), name='payhero-subscription-webhook'),
    path('hotspot/', PayHeroHotspotWebhookView.as_view(), name='payhero-hotspot-webhook'),
    path('billing/', PayHeroBillingWebhookView.as_view(), name='payhero-billing-webhook'),
]

# ==========================
# M-Pesa Webhook URLs (PUBLIC - no auth)
# These receive callbacks from Safaricom
# ==========================
mpesa_webhook_urlpatterns = [
    path('c2b-callback/', MpesaC2BWebhookView.as_view(), name='mpesa-c2b-callback'),
]

# Combine all URL patterns for easy inclusion in main urls.py
hotspot_all_urlpatterns = hotspot_urlpatterns + hotspot_admin_urlpatterns

# Combine all webhook URL patterns
webhook_all_urlpatterns = webhook_urlpatterns + mpesa_webhook_urlpatterns

app_name = 'billing'