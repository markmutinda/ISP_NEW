from django.urls import path, include
from rest_framework.routers import DefaultRouter

from apps.customers.views.customer_views import (
    CustomerViewSet, CustomerAddressViewSet,
    CustomerDocumentViewSet, NextOfKinViewSet,
    CustomerNotesViewSet
)
from apps.customers.views.service_views import ServiceConnectionViewSet
from apps.customers.views.onboarding_views import (
    OnboardingWizardView, DocumentUploadView,
    OnboardingCompleteView, onboarding_checklist
)

# Create routers
customer_router = DefaultRouter()
customer_router.register(r'', CustomerViewSet, basename='customer')

# Nested routers for customer-related resources
address_router = DefaultRouter()
address_router.register(r'addresses', CustomerAddressViewSet, basename='address')

document_router = DefaultRouter()
document_router.register(r'documents', CustomerDocumentViewSet, basename='document')

kin_router = DefaultRouter()
kin_router.register(r'next-of-kin', NextOfKinViewSet, basename='kin')

notes_router = DefaultRouter()
notes_router.register(r'notes', CustomerNotesViewSet, basename='note')

service_router = DefaultRouter()
service_router.register(r'services', ServiceConnectionViewSet, basename='service')

urlpatterns = [
    # Customer management endpoints
    path('', include(customer_router.urls)),
    
    # Customer-specific endpoints
    path('<int:customer_pk>/', include([
        # Address management
        path('', include(address_router.urls)),
        
        # Document management
        path('', include(document_router.urls)),
        
        # Next of kin management
        path('', include(kin_router.urls)),
        
        # Notes management
        path('', include(notes_router.urls)),
        
        # Service management
        path('', include(service_router.urls)),
        
        # Onboarding
        path('onboarding/complete/', 
             OnboardingCompleteView.as_view(), 
             name='onboarding-complete'),
        path('onboarding/checklist/', 
             onboarding_checklist, 
             name='onboarding-checklist'),
        path('documents/upload/', 
             DocumentUploadView.as_view(), 
             name='document-upload'),
    ])),
    
    # Onboarding wizard
    path('onboarding/wizard/', 
         OnboardingWizardView.as_view(), 
         name='onboarding-wizard'),
    
    # Service endpoints (global)
    path('services/pending-activations/', 
         ServiceConnectionViewSet.as_view({'get': 'pending_activations'}), 
         name='pending-activations'),
]