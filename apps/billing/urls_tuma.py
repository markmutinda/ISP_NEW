# apps/billing/urls_tuma.py
from django.urls import path
from .views.tuma_views import (
    TumaBanksView, TumaCreateChildBusinessView, TumaTenantModeView, TumaInitiatePaymentView
)
from .views.tuma_webhook_views import TumaWebhookView

urlpatterns = [
    path('banks/', TumaBanksView.as_view(), name='tuma-banks'),
    path('child-business/', TumaCreateChildBusinessView.as_view(), name='tuma-child-business'),
    path('mode/', TumaTenantModeView.as_view(), name='tuma-mode'),
    path('initiate/', TumaInitiatePaymentView.as_view(), name='tuma-initiate'),
    path('callback/', TumaWebhookView.as_view(), name='tuma-callback'),
]