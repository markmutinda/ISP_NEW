"""
URL configuration for core app
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from . import views
from .views_support import SupportChatDemoView
from .webauthn_views import (
    PasskeyRegisterOptionsView,
    PasskeyRegisterVerifyView,
    PasskeyLoginOptionsView,
    PasskeyLoginVerifyView,
    PasskeyListView,     # NEW
    PasskeyDeleteView,   # NEW
)

router = DefaultRouter()
router.register(r'users', views.UserViewSet, basename='user')
router.register(r'companies', views.CompanyViewSet, basename='company')
router.register(r'tenants', views.TenantViewSet, basename='tenant')
router.register(r'settings', views.SystemSettingsViewSet, basename='setting')
router.register(r'audit-logs', views.AuditLogViewSet, basename='auditlog')
router.register(r'role-access', views.RoleAccessPolicyViewSet, basename='role-access')

urlpatterns = [
    # Authentication endpoints
    path('auth/register/', views.RegisterView.as_view(), name='register'),
    path('auth/login/', views.CustomTokenObtainPairView.as_view(), name='login'),
    path('auth/login/otp/resend/', views.ResendLoginOTPView.as_view(), name='login-otp-resend'),
    path('auth/login/legacy/', views.LoginView.as_view(), name='login_legacy'),
    path('auth/logout/', views.LogoutView.as_view(), name='logout'),
    path('auth/token/refresh/', views.CustomTokenRefreshView.as_view(), name='token_refresh'),
    path('auth/change-password/', views.PasswordChangeView.as_view(), name='change-password'),
    path('settings/', views.GlobalSystemSettingsView.as_view(), name='system-settings'),
    path('branding/', views.TenantBrandingView.as_view(), name='tenant-branding'),
    path('companies/register/', views.CompanyRegisterView.as_view(), name='company-register'),
    path('companies/register/status/', views.CompanyRegistrationStatusView.as_view(), name='company-register-status'),

    # Email verification
    path('auth/verify-email/<uuid:token>/', views.VerifyEmailView.as_view(), name='verify_email'),
    path('auth/resend-verification/', views.ResendVerificationView.as_view(), name='resend_verification'),
    
    # Profile management
    path('profile/', views.ProfileView.as_view(), name='profile'),
    
    # Dashboard
    path('dashboard/', views.DashboardView.as_view(), name='dashboard'),
    
    # ============================================================
    # NEW: Unified Dashboard - Single endpoint replacing 8+ calls
    # ============================================================
    path('dashboard/unified/', views.UnifiedDashboardView.as_view(), name='dashboard-unified'),
    
    # Health check
    path('health/', views.health_check, name='health_check'),
    
    # Changelog endpoint for ISP dashboards (read-only)
    path('changelogs/', views.PlatformChangelogView.as_view(), name='platform-changelogs'),
    
    # Community Feature Requests
    path('feature-requests/', views.CommunityFeatureRequestView.as_view(), name='feature-requests'),
    path('feature-requests/<int:pk>/toggle-upvote/', views.ToggleUpvoteView.as_view(), name='toggle-upvote'),
    path('support-chat/', SupportChatDemoView.as_view(), name='support-chat'),
    
    # OTP endpoints
    path('auth/otp/send/', views.SendOTPView.as_view(), name='send-otp'),
    path('auth/otp/verify/', views.VerifyOTPView.as_view(), name='verify-otp'),
    
    # Tenant lead management and public lead capture
    path('leads/', views.TenantLeadListView.as_view(), name='tenant-leads'),
    path('leads/stats/', views.TenantLeadStatsView.as_view(), name='tenant-lead-stats'),
    path('leads/<int:pk>/', views.TenantLeadDetailView.as_view(), name='tenant-lead-detail'),
    path('leads/submit/', views.SubmitLeadView.as_view(), name='submit-lead'),
    
    # ── Passkey (WebAuthn) endpoints ──
    path('auth/passkey/register/options/', PasskeyRegisterOptionsView.as_view(), name='passkey-register-options'),
    path('auth/passkey/register/verify/', PasskeyRegisterVerifyView.as_view(), name='passkey-register-verify'),
    path('auth/passkey/login/options/', PasskeyLoginOptionsView.as_view(), name='passkey-login-options'),
    path('auth/passkey/login/verify/', PasskeyLoginVerifyView.as_view(), name='passkey-login-verify'),
    
    # ── Passkey Management (authenticated) ──
    path('auth/passkey/list/', PasskeyListView.as_view(), name='passkey-list'),
    path('auth/passkey/delete/<int:pk>/', PasskeyDeleteView.as_view(), name='passkey-delete'),
    
    # Include router URLs
    path('', include(router.urls)),
]