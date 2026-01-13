from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import SupportTicketViewSet

router = DefaultRouter()
router.register(r'tickets', SupportTicketViewSet, basename='ticket')

# Custom URL patterns to match frontend requirements
urlpatterns = [
    path('', include(router.urls)),
    # Additional endpoints as per frontend requirements
    path('tickets/<int:pk>/assign/', SupportTicketViewSet.as_view({'post': 'assign'}), name='ticket-assign'),
    path('tickets/<int:pk>/status/', SupportTicketViewSet.as_view({'post': 'status'}), name='ticket-status'),
    path('tickets/<int:pk>/reply/', SupportTicketViewSet.as_view({'post': 'reply'}), name='ticket-reply'),
    path('tickets/<int:pk>/escalate/', SupportTicketViewSet.as_view({'post': 'escalate'}), name='ticket-escalate'),
    path('tickets/<int:pk>/messages/', SupportTicketViewSet.as_view({'get': 'messages'}), name='ticket-messages'),
    path('tickets/stats/', SupportTicketViewSet.as_view({'get': 'stats'}), name='ticket-stats'),
    path('tickets/my/', SupportTicketViewSet.as_view({'get': 'my'}), name='my-tickets'),
]