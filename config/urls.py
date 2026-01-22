"""
Main URL configuration for ISP Management System
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi
from apps.core.admin import admin_site

# Schema view for API documentation
schema_view = get_schema_view(
    openapi.Info(
        title="ISP Management System API",
        default_version='v1',
        description="API documentation for ISP Management System",
        terms_of_service="https://www.example.com/terms/",
        contact=openapi.Contact(email="support@ispmanagement.com"),
        license=openapi.License(name="Proprietary"),
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
)

# API URL Patterns
api_urlpatterns = [
    # Core app (Authentication, Users, System)
    path('core/', include('apps.core.urls')),
    
    # Customers app (Phase 2 - Customer Management)
    path('customers/', include('apps.customers.urls')),
    
    # Network app (Phase 3 - Network Management)
    path('network/', include('apps.network.urls')),
    
    # Bandwidth app (Phase 4 - Bandwidth Management)
    path('api/bandwidth/', include('apps.bandwidth.urls')),  
    
    # Billing app (Phase 5 - Billing & Finance)
     path('billing/', include('apps.billing.urls')),
    
    # Support app (Phase 6 - Support Ticketing)
     path('support/', include('apps.support.urls')),
    
    # Analytics app (Phase 7 - Reports & Analytics)
     path('analytics/', include('apps.analytics.urls')),  
    
    # Staff app (Phase 8 - Staff Management)
    path('api/staff/', include('apps.staff.urls')),
    
    # Self-Service app (Phase 9 - Customer Portal)
    #path('api/self-service/', include('apps.self_service.urls')),
    
    # Inventory app (Phase 10 - Inventory Management)
     path('inventory/', include('apps.inventory.urls')),
    
    # Notifications app (Phase 11 - Alerts & Notifications)
     path('notifications/', include('apps.notifications.urls')),


    # Messaging app (Phase 12 - SMS/Email Messaging) 
    path('messaging/', include('apps.messaging.urls')),
    
    # ─────────────────────────────────────────────────────────────
    # Subscriptions app (Netily Platform Subscriptions)
    # ─────────────────────────────────────────────────────────────
    path('subscriptions/', include('apps.subscriptions.urls')),
    
    # ISP Payout Configuration (under core/)
    # These use urls from subscriptions.urls.payout_urlpatterns
]

# Hotspot URLs (PUBLIC - no auth required for captive portal)
from apps.billing.urls import hotspot_urlpatterns, webhook_urlpatterns

hotspot_api_urlpatterns = [
    path('hotspot/', include((hotspot_urlpatterns, 'hotspot'))),
]

# PayHero Webhooks (PUBLIC - callbacks from PayHero)
webhook_api_urlpatterns = [
    path('webhooks/payhero/', include((webhook_urlpatterns, 'webhooks'))),
]

# Main URL Patterns
urlpatterns = [
    # Admin URLs (using custom admin site)
    path('admin/', admin_site.urls),
    
    # API URLs (versioned)
    path('api/v1/', include(api_urlpatterns)),
    
    # Hotspot API (PUBLIC - for captive portal)
    path('api/v1/', include(hotspot_api_urlpatterns)),
    
    # PayHero Webhooks (PUBLIC - callbacks)
    path('api/v1/', include(webhook_api_urlpatterns)),
    
    # API Documentation
    path('swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
    path('redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
    path('swagger.json/', schema_view.without_ui(cache_timeout=0), name='schema-json'),
    
    # Health check
    path('health/', include('health_check.urls')),
]

# Authentication URLs (for Django REST Framework browsable API)
urlpatterns += [
    path('api-auth/', include('rest_framework.urls')),
]

# Serve static and media files in development
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    
    # Debug toolbar
    try:
        import debug_toolbar
        urlpatterns = [
            path('__debug__/', include(debug_toolbar.urls)),
        ] + urlpatterns
    except ImportError:
        pass

# Custom error handlers
#handler400 = 'apps.core.views.error_handlers.bad_request'
#handler403 = 'apps.core.views.error_handlers.permission_denied'
#handler404 = 'apps.core.views.error_handlers.page_not_found'
#handler500 = 'apps.core.views.error_handlers.server_error'
