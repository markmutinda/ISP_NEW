from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views.InvoiceViews import PlanViewSet, BillingCycleViewSet, InvoiceViewSet, InvoiceItemViewSet
from .views.PaymentViews import PaymentViewSet, MpesaConfigurationViewSet, MpesaTransactionViewSet
from .views.VoucherViews import VoucherBatchViewSet, VoucherViewSet

from .views.hotspot_views import (
    CaptivePortalView, HotspotPlansView, HotspotPurchaseView, HotspotPurchaseStatusView,
    HotspotVoucherRedeemView, HotspotPhoneReconnectView, HotspotFreeTrialView  # ADDED: HotspotFreeTrialView
)

from .views.cloud_portal_views import (
    HotspotLoginPageView,
    HotspotAutoLoginView,
    HotspotReturnTripView,
    HotspotDeviceAuthView,
    HotspotDeviceAuthStatusView,
    # REMOVED: GenerateTVCodeView, VerifyTVCodeView
)
from .views.hotspot_admin_views import (
    HotspotPlanViewSet,
    HotspotSessionViewSet,
    HotspotBrandingView,
    HotspotDashboardView,
    GlobalHotspotPlanListView,
    HotspotClientViewSet,
    ActiveSubscriptionsView,
    RouterIncomeView,
    HotspotSessionExtendView,
    HotspotClientDetailView,
    HotspotNetworkScanView,  # ADDED: New network scan view
)
from .views.hotspot_voucher_admin_views import (
    HotspotVoucherGenerateView,
    HotspotVoucherListView,
    HotspotVoucherDetailView,
)
from .views.customer_payment_views import (
    InitiateCustomerPaymentView,
    CustomerPaymentStatusView,
    CustomerPaymentMethodsView,
    PaymentMethodDetailView,
    PaymentMethodToggleActiveView,
)
from .views.webhook_views import (
    MpesaC2BWebhookView,
)

# ==========================
# Tuma URLs (New Payment Gateway)
# ==========================
from .views.tuma_views import (
    TumaBanksView, 
    TumaCreateChildBusinessView, 
    TumaTenantModeView, 
    TumaInitiatePaymentView
)
from .views.tuma_webhook_views import TumaWebhookView
from .views.netily_system_payment_views import (
    NetilySystemPaymentCallbackView,
    NetilySystemPaymentInitiateView,
    NetilySystemPaymentStatusView,
)
# 🚨 NEW: Import Netily Paybill webhook view
from .views.netily_paybill_webhook_views import NetilyPaybillWebhookView

# ==========================
# Hotspot Ad-Sponsored URLs
# ==========================
from .views.hotspot_ad_views import (
    HotspotAdServeView,
    HotspotAdGrantView,
    HotspotAdMediaView,
    HotspotAdAdminViewSet,
)

# ==========================
# Hotspot Loyalty URLs
# ==========================
from .views.hotspot_loyalty_views import (
    HotspotLoyaltyInfoView,
    HotspotLoyaltyRedeemView,
)

# ==========================
# Invoice Settings & Utilities (NEW)
# ==========================
from .views.invoice_settings_views import (
    InvoiceSettingsView, 
    CustomerSearchView, 
    InvoicePDFView,
    HotspotPruneSettingsView,   # <-- add
)

router = DefaultRouter()

# Invoice URLs
router.register(r'plans', PlanViewSet, basename='plan')
router.register(r'billing-cycles', BillingCycleViewSet, basename='billing-cycle')
router.register(r'invoices', InvoiceViewSet, basename='invoice')
router.register(r'invoice-items', InvoiceItemViewSet, basename='invoice-item')

# Payment URLs
router.register(r'payments', PaymentViewSet, basename='payment')

# M-Pesa Configuration URLs
router.register(r'mpesa-config', MpesaConfigurationViewSet, basename='mpesa-config')
router.register(r'mpesa-transactions', MpesaTransactionViewSet, basename='mpesa-transaction')

# Voucher URLs
router.register(r'voucher-batches', VoucherBatchViewSet, basename='voucher-batch')
router.register(r'vouchers', VoucherViewSet, basename='voucher')

# ==========================
# Hotspot URLs (PUBLIC - no auth)
# ==========================
hotspot_urlpatterns = [
    path('captive-portal/', CaptivePortalView.as_view(), name='hotspot-captive-portal'),
    path('routers/<int:router_id>/plans/', HotspotPlansView.as_view(), name='hotspot-plans'),
    path('purchase/', HotspotPurchaseView.as_view(), name='hotspot-purchase'),
    path('purchase/<str:session_id>/status/', HotspotPurchaseStatusView.as_view(), name='hotspot-status'),
    path('login-page/<int:router_id>/', HotspotLoginPageView.as_view(), name='hotspot-login-page'),
    path('auto-login/', HotspotAutoLoginView.as_view(), name='hotspot-auto-login'),
    path('return-trip/<str:session_id>/', HotspotReturnTripView.as_view(), name='hotspot-return-trip'),
    path('device-auth/request/', HotspotDeviceAuthView.as_view(), name='hotspot-device-auth-request'),
    path('device-auth/authorize/', HotspotDeviceAuthView.as_view(), name='hotspot-device-auth-authorize'),
    path('device-auth/status/', HotspotDeviceAuthStatusView.as_view(), name='hotspot-device-auth-status'),
    path('voucher-redeem/', HotspotVoucherRedeemView.as_view(), name='hotspot-voucher-redeem'),
    path('phone-reconnect/', HotspotPhoneReconnectView.as_view(), name='hotspot-phone-reconnect'),
    # FREE TRIAL URL - ADDED
    path('free-trial/', HotspotFreeTrialView.as_view(), name='hotspot-free-trial'),
    # REMOVED: path('tv/generate-code/', GenerateTVCodeView.as_view(), name='hotspot-tv-generate-code'),
    # REMOVED: path('tv/verify-code/', VerifyTVCodeView.as_view(), name='hotspot-tv-verify-code'),
    # ADDED: Network scan for TV MAC detection
    path('scan-devices/', HotspotNetworkScanView.as_view(), name='hotspot-scan-devices'),
    path('ads/serve/', HotspotAdServeView.as_view(), name='hotspot-ad-serve'),
    path('ads/grant-access/', HotspotAdGrantView.as_view(), name='hotspot-ad-grant'),
    path('ads/media/<int:pk>/', HotspotAdMediaView.as_view(), name='hotspot-ad-media'),
    path('loyalty-info/', HotspotLoyaltyInfoView.as_view(), name='hotspot-loyalty-info'),
    path('loyalty-redeem/', HotspotLoyaltyRedeemView.as_view(), name='hotspot-loyalty-redeem'),
]

# ==========================
# Hotspot Admin URLs (AUTHENTICATED)
# ==========================
hotspot_admin_urlpatterns = [
    # Dashboard
    path('dashboard/', HotspotDashboardView.as_view(), name='hotspot-dashboard'),
    path('admin/plans/', GlobalHotspotPlanListView.as_view(), name='hotspot-admin-all-plans'),
    
    # ============================================================
    # HOTSPOT CLIENTS - FIXED: HotspotClientDetailView handles both GET and DELETE
    # ============================================================
    path('admin/clients/', 
         HotspotClientViewSet.as_view({'get': 'list'}), 
         name='hotspot-admin-clients'),
    
    # This single URL handles both:
    # - GET /admin/clients/<int:id>/  → returns client info + sessions
    # - DELETE /admin/clients/<int:id>/ → deletes the client
    path('admin/clients/<int:id>/', 
         HotspotClientDetailView.as_view(), 
         name='hotspot-admin-client-detail'),
    
    # Keep this for backward compatibility or remove if not needed
    path('admin/clients/<int:id>/sessions/',
         HotspotClientDetailView.as_view(),
         name='hotspot-client-sessions'),
    
    # ACTIVE SUBSCRIPTIONS
    path('admin/active-subscriptions/', 
         ActiveSubscriptionsView.as_view(), 
         name='hotspot-active-subscriptions'),
    
    # ROUTER INCOME
    path('admin/routers/<int:router_id>/income/',
         RouterIncomeView.as_view(),
         name='hotspot-router-income'),
    
    # SESSION EXTENSION (Admin only)
    path('admin/sessions/<str:session_id>/extend/',
         HotspotSessionExtendView.as_view(),
         name='hotspot-session-extend'),
    
    # Plans CRUD (per-router)
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
    
    # Sessions (per-router)
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
    
    # ============================================================
    # HOTSPOT VOUCHER ADMIN ROUTES - ORDER MATTERS!
    # Generate (POST) and List (GET) must come BEFORE detail route
    # ============================================================
    # 1. Generate vouchers - MUST COME FIRST (exact match)
    path('admin/vouchers/generate/', 
         HotspotVoucherGenerateView.as_view(), 
         name='hotspot-admin-voucher-generate'),
    
    # 2. List vouchers - exact match
    path('admin/vouchers/', 
         HotspotVoucherListView.as_view(), 
         name='hotspot-admin-voucher-list'),
    
    # 3. Detail route (for edit/delete) - uses int:pk, comes LAST
    # Note: Voucher IDs are integers (AutoField)
    path('admin/vouchers/<int:pk>/',
         HotspotVoucherDetailView.as_view(),
         name='hotspot-admin-voucher-detail'),
    
    # AD MANAGEMENT
    path('admin/ads/',
         HotspotAdAdminViewSet.as_view({'get': 'list', 'post': 'create'}),
         name='hotspot-admin-ads'),
    path('admin/ads/storage/',
         HotspotAdAdminViewSet.as_view({'get': 'storage'}),
         name='hotspot-admin-ads-storage'),
    path('admin/ads/<int:pk>/',
         HotspotAdAdminViewSet.as_view({'get': 'retrieve', 'patch': 'partial_update', 'delete': 'destroy'}),
         name='hotspot-admin-ad-detail'),
    path('admin/ads/<int:pk>/toggle-active/',
         HotspotAdAdminViewSet.as_view({'post': 'toggle_active'}),
         name='hotspot-admin-ad-toggle'),
]

# ==========================
# M-Pesa Webhook URLs (PUBLIC - no auth)
# ==========================
mpesa_webhook_urlpatterns = [
    path('c2b-callback/', MpesaC2BWebhookView.as_view(), name='mpesa-c2b-callback'),
    path('c2b-validation/', MpesaC2BWebhookView.as_view(), name='mpesa-c2b-validation'),
]

# ==========================
# Tuma Webhook URLs (PUBLIC - no auth)
# ==========================
tuma_webhook_urlpatterns = [
    path('tuma/callback/', TumaWebhookView.as_view(), name='tuma-callback'),
]

# ==========================
# MAIN URL PATTERNS (must be at the bottom)
# ==========================
urlpatterns = [
    path('', include(router.urls)),

    # ==========================
    # INVOICE SETTINGS & UTILITIES (NEW - Added above Tuma)
    # ==========================
    path('invoice-settings/', InvoiceSettingsView.as_view(), name='invoice-settings'),
    path('customers/search/', CustomerSearchView.as_view(), name='invoice-customer-search'),
    path('invoices/<int:invoice_id>/pdf/', InvoicePDFView.as_view(), name='invoice-pdf'),
    
    # ==========================
    # HOTSPOT PRUNE SETTINGS
    # ==========================
    path('hotspot-prune-settings/', HotspotPruneSettingsView.as_view(), name='hotspot-prune-settings'),

    # ==========================
    # Tuma Endpoints
    # ==========================
    path('tuma/banks/', TumaBanksView.as_view(), name='tuma-banks'),
    path('tuma/child-business/', TumaCreateChildBusinessView.as_view(), name='tuma-child-business'),
    path('tuma/mode/', TumaTenantModeView.as_view(), name='tuma-mode'),
    path('tuma/initiate/', TumaInitiatePaymentView.as_view(), name='tuma-initiate'),
    path('tuma/callback/', TumaWebhookView.as_view(), name='tuma-callback'),
    
    # ==========================
    # Netily System Payment Endpoints
    # ==========================
    path('netily-system-payment/initiate/', NetilySystemPaymentInitiateView.as_view(), name='netily-system-payment-initiate'),
    path('netily-system-payment/status/<str:checkout_request_id>/', NetilySystemPaymentStatusView.as_view(), name='netily-system-payment-status'),
    path('netily-system-payment/callback/', NetilySystemPaymentCallbackView.as_view(), name='netily-system-payment-callback'),

    # ==========================
    # 🚨 NETILY PAYBILL WEBHOOK (replaces Tuma passthrough)
    # ==========================
    path('netily-paybill/callback/', NetilyPaybillWebhookView.as_view(), name='netily-paybill-callback'),

    # ==========================
    # M-Pesa Endpoints
    # ==========================
    path('daraja/c2b-callback/', MpesaC2BWebhookView.as_view(), name='mpesa-c2b-callback'),
    path('daraja/c2b-validation/', MpesaC2BWebhookView.as_view(), name='mpesa-c2b-validation'),
    path('mpesa/callback/', PaymentViewSet.as_view({'post': 'mpesa_callback'}), name='mpesa-callback-legacy'),
    
    path('mpesa-config/<int:pk>/test/', 
         MpesaConfigurationViewSet.as_view({'post': 'test_connection'}), 
         name='mpesa-config-test'),
    path('mpesa-config/<int:pk>/test_connection/', 
         MpesaConfigurationViewSet.as_view({'post': 'test_connection'}), 
         name='mpesa-config-test-connection'),
    path('mpesa-config/<int:pk>/set-default/', 
         MpesaConfigurationViewSet.as_view({'post': 'set_default'}), 
         name='mpesa-config-set-default'),
    path('mpesa-config/<int:pk>/set_default/', 
         MpesaConfigurationViewSet.as_view({'post': 'set_default'}), 
         name='mpesa-config-set-default-legacy'),
    path('mpesa-config/<int:pk>/toggle-active/', 
         MpesaConfigurationViewSet.as_view({'post': 'toggle_active'}), 
         name='mpesa-config-toggle-active'),
    path('mpesa-config/<int:pk>/activate-as-primary/',
         MpesaConfigurationViewSet.as_view({'post': 'activate_as_primary'}),
         name='mpesa-config-activate-primary'),
    path('mpesa-config/<int:pk>/deactivate-daraja/',
         MpesaConfigurationViewSet.as_view({'post': 'deactivate_daraja'}),
         name='mpesa-config-deactivate-daraja'),
    path('mpesa-config/active/', 
         MpesaConfigurationViewSet.as_view({'get': 'active'}), 
         name='mpesa-config-active'),
    path('mpesa-config/default/', 
         MpesaConfigurationViewSet.as_view({'get': 'default'}), 
         name='mpesa-config-default'),
    path('mpesa-transactions/<int:pk>/status/', 
         MpesaTransactionViewSet.as_view({'get': 'status'}), 
         name='mpesa-transaction-status'),

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
    # Customer Payment Initiation
    # ==========================
    path('payments/initiate/', InitiateCustomerPaymentView.as_view(), name='initiate-payment'),
    path('payments/<int:payment_id>/status/', CustomerPaymentStatusView.as_view(), name='payment-status'),
    path('payment-methods/', CustomerPaymentMethodsView.as_view(), name='payment-methods'),
    path('payment-methods/<int:pk>/', PaymentMethodDetailView.as_view(), name='payment-method-detail'),
    path('payment-methods/<int:pk>/toggle_active/', PaymentMethodToggleActiveView.as_view(), name='payment-method-toggle-active'),

    # ==========================
    # Hotspot Public URLs (mounted under /hotspot/)
    # ==========================
    path('hotspot/', include(hotspot_urlpatterns)),
]

# Combine all URL patterns for convenience (optional)
hotspot_all_urlpatterns = hotspot_urlpatterns + hotspot_admin_urlpatterns
webhook_all_urlpatterns = mpesa_webhook_urlpatterns + tuma_webhook_urlpatterns

app_name = 'billing'