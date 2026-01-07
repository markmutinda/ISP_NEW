"""
URL configuration for core app
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from . import views

router = DefaultRouter()
router.register(r'users', views.UserViewSet, basename='user')
router.register(r'companies', views.CompanyViewSet, basename='company')
router.register(r'tenants', views.TenantViewSet, basename='tenant')
router.register(r'settings', views.SystemSettingsViewSet, basename='setting')
router.register(r'audit-logs', views.AuditLogViewSet, basename='auditlog')

urlpatterns = [
    # Authentication endpoints
    path('auth/register/', views.RegisterView.as_view(), name='register'),
    path('auth/login/', views.CustomTokenObtainPairView.as_view(), name='login'),
    path('auth/login/legacy/', views.LoginView.as_view(), name='login_legacy'),
    path('auth/logout/', views.LogoutView.as_view(), name='logout'),
    path('auth/token/refresh/', views.CustomTokenRefreshView.as_view(), name='token_refresh'),
    path('auth/change-password/', views.PasswordChangeView.as_view(), name='change-password'),
    path('settings/', views.GlobalSystemSettingsView.as_view(), name='system-settings'),
    
    # Email verification
    path('auth/verify-email/<uuid:token>/', views.VerifyEmailView.as_view(), name='verify_email'),
    path('auth/resend-verification/', views.ResendVerificationView.as_view(), name='resend_verification'),
    
    # Profile management
    path('profile/', views.ProfileView.as_view(), name='profile'),
    
    # Dashboard
    path('dashboard/', views.DashboardView.as_view(), name='dashboard'),
    
    # Health check
    path('health/', views.health_check, name='health_check'),
    
    # Include router URLs
    path('', include(router.urls)),
]