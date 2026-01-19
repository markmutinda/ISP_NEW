# apps/messaging/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    SMSMessageViewSet,
    SMSTemplateViewSet,
    SMSCampaignViewSet,
    SMSStatsView,
    SMSBalanceView,
)

router = DefaultRouter()
router.register(r'sms', SMSMessageViewSet, basename='sms-message')
router.register(r'templates', SMSTemplateViewSet, basename='sms-template')
router.register(r'campaigns', SMSCampaignViewSet, basename='sms-campaign')

urlpatterns = [
    # All standard CRUD from router
    path('', include(router.urls)),

    # Bulk send (custom action)
    path('sms/bulk/', SMSMessageViewSet.as_view({'post': 'bulk_send'}), name='sms-bulk-send'),

    # Retry single message
    path('sms/<int:pk>/retry/', SMSMessageViewSet.as_view({'post': 'retry'}), name='sms-retry'),

    # Start / cancel campaign
    path('campaigns/<int:pk>/start/', SMSCampaignViewSet.as_view({'post': 'start'}), name='campaign-start'),
    path('campaigns/<int:pk>/cancel/', SMSCampaignViewSet.as_view({'post': 'cancel'}), name='campaign-cancel'),

    # Missing stats & balance (these are the ones causing 404)
    path('sms/stats/', SMSStatsView.as_view(), name='sms-stats'),
    path('sms/balance/', SMSBalanceView.as_view(), name='sms-balance'),
]
