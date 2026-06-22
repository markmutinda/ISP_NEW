# apps/messaging/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    SMSMessageViewSet,
    SMSTemplateViewSet,
    SMSCampaignViewSet,
    SMSStatsView,
    SMSBalanceView,
    SMSGatewayConfigViewSet,
    SMSNotificationSettingsView,
    SMSWalletView,
    SMSTopupInitiateView,
    SMSTopupCallbackView,
    CustomerSearchView,
    # CampaignSendToGroupView removed — use SMSCampaignViewSet.send_to_group instead
)

router = DefaultRouter()
router.register(r'sms', SMSMessageViewSet, basename='sms-message')
router.register(r'templates', SMSTemplateViewSet, basename='sms-template')
router.register(r'campaigns', SMSCampaignViewSet, basename='sms-campaign')
router.register(r'gateway', SMSGatewayConfigViewSet, basename='sms-gateway')

urlpatterns = [
    # ============================================================
    # CRITICAL: Explicit paths MUST come BEFORE include(router.urls)
    # because the router registers 'sms/' which catches ALL requests
    # starting with 'sms/' including 'sms/stats/' and 'sms/balance/'.
    # Django matches URLs top-to-bottom, so exact paths win.
    # ============================================================
    
    # Stats & balance (live provider balance)
    path('sms/stats/', SMSStatsView.as_view(), name='sms-stats'),
    path('sms/balance/', SMSBalanceView.as_view(), name='sms-balance'),
    
    # Single / bulk send
    path('sms/bulk/', SMSMessageViewSet.as_view({'post': 'bulk_send'}), name='sms-bulk-send'),
    path('sms/<int:pk>/retry/', SMSMessageViewSet.as_view({'post': 'retry'}), name='sms-retry'),

    # ============================================================
    # Router URLs (must come after the explicit paths above)
    # ============================================================
    path('', include(router.urls)),

    # Campaign control
    path('campaigns/<int:pk>/start/', SMSCampaignViewSet.as_view({'post': 'start'}), name='campaign-start'),
    path('campaigns/<int:pk>/cancel/', SMSCampaignViewSet.as_view({'post': 'cancel'}), name='campaign-cancel'),

    # NOTE: campaigns/send-to-group/ is now handled by SMSCampaignViewSet.send_to_group
    # via the router at campaigns/send-to-group/ (the router generates this from the @action decorator).
    # The separate CampaignSendToGroupView has been removed to eliminate ambiguity.

    # Gateway CRUD + test + upsert
    path('gateway/<int:pk>/activate/', SMSGatewayConfigViewSet.as_view({'post': 'activate'}), name='gateway-activate'),
    path('gateway/<int:pk>/test/', SMSGatewayConfigViewSet.as_view({'post': 'test_connection'}), name='gateway-test'),
    path('gateway/providers/', SMSGatewayConfigViewSet.as_view({'get': 'list_providers'}), name='gateway-providers'),
    path('gateway/save/', SMSGatewayConfigViewSet.as_view({'post': 'save_config', 'patch': 'save_config'}), name='gateway-save'),

    # Notification settings (hotspot + pppoe toggles)
    path('notification-settings/', SMSNotificationSettingsView.as_view(), name='sms-notification-settings'),

    # Internal wallet
    path('wallet/', SMSWalletView.as_view(), name='sms-wallet'),
    path('topup/initiate/', SMSTopupInitiateView.as_view(), name='sms-topup-initiate'),
    path('topup/callback/', SMSTopupCallbackView.as_view(), name='sms-topup-callback'),  # public

    # Customer search for SMS compose dialog
    path('customers/search/', CustomerSearchView.as_view(), name='customer-search'),
]