from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'templates', views.NotificationTemplateViewSet, basename='notification-template')
router.register(r'notifications', views.NotificationViewSet, basename='notification')
router.register(r'alert-rules', views.AlertRuleViewSet, basename='alert-rule')
router.register(r'preferences', views.NotificationPreferenceViewSet, basename='notification-preference')
router.register(r'bulk-notifications', views.BulkNotificationViewSet, basename='bulk-notification')

# Create a simple NotificationLogViewSet in the same file
from rest_framework import viewsets, filters
from django_filters.rest_framework import DjangoFilterBackend
from .models import NotificationLog
from .serializers import NotificationLogSerializer
from .permissions import IsAdminOrStaff
from rest_framework.permissions import IsAuthenticated

class NotificationLogViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for viewing notification logs (read-only)"""
    queryset = NotificationLog.objects.all()
    serializer_class = NotificationLogSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['action', 'user']
    search_fields = ['details', 'ip_address']

# Register the NotificationLogViewSet
router.register(r'logs', NotificationLogViewSet, basename='notification-log')

urlpatterns = [
    path('', include(router.urls)),
    
    # Additional endpoints
    path('send/', views.SendNotificationView.as_view(), name='send-notification'),
    path('stats/', views.NotificationStatsView.as_view(), name='notification-stats'),
    path('self-service/', views.SelfServiceNotificationView.as_view(), name='self-service-notifications'),
]

