from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'tiers', views.LoyaltyTierViewSet, basename='loyalty-tier')
router.register(r'members', views.LoyaltyMemberViewSet, basename='loyalty-member')
router.register(r'rewards', views.LoyaltyRewardViewSet, basename='loyalty-reward')
router.register(r'transactions', views.PointsTransactionViewSet, basename='loyalty-transaction')
router.register(r'rules', views.PointsRuleViewSet, basename='loyalty-rule')

urlpatterns = [
    path('settings/', views.LoyaltySettingsView.as_view(), name='loyalty-settings'),
    path('stats/', views.loyalty_stats, name='loyalty-stats'),
    path('leaderboard/', views.loyalty_leaderboard, name='loyalty-leaderboard'),
    path('', include(router.urls)),
]
