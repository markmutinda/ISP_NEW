from django.urls import path

from . import views

app_name = "affiliate"

urlpatterns = [
    path("register/", views.AffiliateRegisterView.as_view(), name="register"),
    path("login/", views.AffiliateLoginView.as_view(), name="login"),
    path("login/otp/resend/", views.AffiliateResendLoginOTPView.as_view(), name="login-otp-resend"),
    path("token/refresh/", views.AffiliateTokenRefreshView.as_view(), name="token-refresh"),
    path("me/", views.AffiliateMeView.as_view(), name="me"),
    path("verify/", views.AffiliateVerificationView.as_view(), name="verify"),
    path("resend-verification/", views.AffiliateResendVerificationView.as_view(), name="resend-verification"),
    path("admin-access/exchange/", views.AffiliateAdminAccessExchangeView.as_view(), name="admin-access-exchange"),
    path("r/<str:code>/click/", views.AffiliateClickView.as_view(), name="click"),
    path("dashboard/", views.AffiliateDashboardView.as_view(), name="dashboard"),
    path("referrals/", views.AffiliateReferralListView.as_view(), name="referrals"),
    path("analytics/", views.AffiliateAnalyticsView.as_view(), name="analytics"),
    path("traffic/", views.AffiliateTrafficView.as_view(), name="traffic"),
    path("payouts/", views.AffiliatePayoutListView.as_view(), name="payouts"),
    path("payment-method/", views.AffiliatePaymentMethodView.as_view(), name="payment-method"),
    path("tiers/", views.AffiliateTierView.as_view(), name="tiers"),
    path("marketing/", views.AffiliateMarketingView.as_view(), name="marketing"),
    path("admin/affiliates/", views.AdminAffiliateListView.as_view(), name="admin-list"),
    path("admin/settings/", views.AdminAffiliateSettingsView.as_view(), name="admin-settings"),
    path("admin/affiliates/<int:pk>/", views.AdminAffiliateDetailView.as_view(), name="admin-detail"),
    path("admin/affiliates/<int:pk>/access/", views.AdminAffiliateAccessView.as_view(), name="admin-access"),
    path("admin/affiliates/<int:affiliate_id>/referrals/", views.AdminAffiliateReferralListView.as_view(), name="admin-referral-create"),
    path("admin/referrals/<int:pk>/", views.AdminReferralDetailView.as_view(), name="admin-referral"),
    path("admin/affiliates/<int:affiliate_id>/payouts/", views.AdminPayoutListView.as_view(), name="admin-payout-create"),
    path("admin/payouts/<int:pk>/", views.AdminPayoutDetailView.as_view(), name="admin-payout-detail"),
    path("admin/export/", views.AdminAffiliateExportView.as_view(), name="admin-export"),
]
