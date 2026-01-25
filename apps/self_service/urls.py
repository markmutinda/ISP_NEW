from django.urls import path
from .views import (
    # Customer Self-Registration & Login (Public)
    CustomerSelfRegisterView,
    CustomerLoginView,
    VerifyPhoneView,
    ResendOTPView,
    AvailablePlansView,
    
    # Customer Dashboard (Authenticated)
    CustomerDashboardView,
    PaymentView,
    PaymentStatusView,
    PaymentRefreshStatusView,
    CustomerPaymentsListView,
    
    # Service Requests
    ServiceRequestListCreateView,
    ServiceRequestDetailView,
    ServiceRequestTypesView,
    
    # Alerts
    CustomerAlertsView,
    MarkAlertReadView,
    MarkAllAlertsReadView,
)

urlpatterns = [
    # ==========================================================================
    # PUBLIC ENDPOINTS (No Authentication Required)
    # ==========================================================================
    
    # Customer self-registration and login
    path('register/', CustomerSelfRegisterView.as_view(), name='customer-register'),
    path('login/', CustomerLoginView.as_view(), name='customer-login'),
    path('verify-phone/', VerifyPhoneView.as_view(), name='verify-phone'),
    path('resend-otp/', ResendOTPView.as_view(), name='resend-otp'),
    
    # Available plans (for registration page)
    path('plans/', AvailablePlansView.as_view(), name='available-plans'),
    
    # ==========================================================================
    # AUTHENTICATED ENDPOINTS (Customer Login Required)
    # ==========================================================================
    
    # Dashboard
    path('dashboard/', CustomerDashboardView.as_view(), name='customer-dashboard'),
    
    # Payments
    path('payments/', CustomerPaymentsListView.as_view(), name='customer-payments-list'),
    path('payments/initiate/', PaymentView.as_view(), name='customer-payment-initiate'),
    path('payments/<uuid:payment_id>/status/', PaymentStatusView.as_view(), name='payment-status'),
    path('payments/<uuid:payment_id>/refresh/', PaymentRefreshStatusView.as_view(), name='payment-refresh'),
    
    # Service Requests
    path('service-requests/', ServiceRequestListCreateView.as_view(), name='service-requests'),
    path('service-requests/<int:pk>/', ServiceRequestDetailView.as_view(), name='service-request-detail'),
    path('service-request-types/', ServiceRequestTypesView.as_view(), name='service-request-types'),
    
    # Alerts
    path('alerts/', CustomerAlertsView.as_view(), name='customer-alerts'),
    path('alerts/<int:pk>/read/', MarkAlertReadView.as_view(), name='mark-alert-read'),
    path('alerts/mark-all-read/', MarkAllAlertsReadView.as_view(), name='mark-all-alerts-read'),
]
