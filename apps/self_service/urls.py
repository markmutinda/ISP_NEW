from django.urls import path
from .views import (
    CustomerDashboardView,
    PaymentView,
    ServiceRequestListCreateView,
    ServiceRequestDetailView,
    ServiceRequestTypesView,
    
)

urlpatterns = [
    # Dashboard
    path('dashboard/', CustomerDashboardView.as_view(), name='customer-dashboard'),
    
    # Usage
   # path('usage/', UsageView.as_view(), name='customer-usage'),
    
    # Payments
    path('payments/', PaymentView.as_view(), name='customer-payments'),
    
    # Service Requests
    path('service-requests/', ServiceRequestListCreateView.as_view(), name='service-requests'),
    path('service-requests/<int:pk>/', ServiceRequestDetailView.as_view(), name='service-request-detail'),
    path('service-request-types/', ServiceRequestTypesView.as_view(), name='service-request-types'),
]
