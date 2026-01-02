from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    TicketCategoryViewSet, TicketStatusViewSet, 
    TicketViewSet, TicketMessageViewSet, TicketActivityViewSet,
    KnowledgeBaseArticleViewSet, FAQViewSet,
    TechnicianViewSet
)

router = DefaultRouter()

# Ticket routes
router.register(r'ticket-categories', TicketCategoryViewSet, basename='ticket-category')
router.register(r'ticket-statuses', TicketStatusViewSet, basename='ticket-status')
router.register(r'tickets', TicketViewSet, basename='ticket')
router.register(r'tickets/(?P<ticket_pk>\d+)/messages', TicketMessageViewSet, basename='ticket-message')
router.register(r'tickets/(?P<ticket_pk>\d+)/activities', TicketActivityViewSet, basename='ticket-activity')

# Knowledge base routes
router.register(r'knowledge-base/articles', KnowledgeBaseArticleViewSet, basename='knowledgebase-article')
router.register(r'knowledge-base/faqs', FAQViewSet, basename='faq')

# Technician routes
router.register(r'technicians', TechnicianViewSet, basename='technician')

urlpatterns = [
    path('', include(router.urls)),
    path('dashboard/stats/', TicketViewSet.as_view({'get': 'dashboard_stats'}), name='support-dashboard-stats'),
]