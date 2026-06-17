"""
Superadmin URL Configuration
─────────────────────────────
All endpoints live under /api/v1/superadmin/
"""

from django.urls import path

from . import views

app_name = "superadmin"

urlpatterns = [
    # Dashboard KPIs
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),

    # Tenant CRUD
    path("tenants/", views.TenantListView.as_view(), name="tenant-list"),
    path("tenants/create/", views.TenantCreateView.as_view(), name="tenant-create"),
    path("tenants/<uuid:pk>/", views.TenantDetailView.as_view(), name="tenant-detail"),
    path("tenants/<uuid:pk>/delete-request/", views.TenantDeletionRequestView.as_view(), name="tenant-delete-request"),
    path("tenants/<uuid:pk>/hard-delete/", views.HardDeleteTenantView.as_view(), name="tenant-hard-delete"),
    path("tenants/<uuid:pk>/suspend/", views.TenantSuspendView.as_view(), name="tenant-suspend"),
    path("tenants/<uuid:pk>/activate/", views.TenantActivateView.as_view(), name="tenant-activate"),
    path("tenants/<uuid:pk>/company/", views.CompanyUpdateView.as_view(), name="company-update"),
    path("tenants/<uuid:pk>/stats/", views.TenantStatsView.as_view(), name="tenant-stats"),
    path("tenants/<uuid:pk>/audit-log/", views.TenantAuditLogView.as_view(), name="tenant-audit-log"),
    path("tenants/<uuid:pk>/routers/", views.TenantRoutersView.as_view(), name="tenant-routers"),
    path("tenants/<uuid:pk>/pppoe-users/", views.TenantPPPoEUsersView.as_view(), name="tenant-pppoe"),
    path("tenants/<uuid:pk>/hotspot-users/", views.TenantHotspotUsersView.as_view(), name="tenant-hotspot"),
    path("tenants/<uuid:pk>/inventory/", views.TenantInventoryView.as_view(), name="tenant-inventory"),
    path("tenants/<uuid:pk>/impersonate/", views.TenantImpersonateView.as_view(), name="tenant-impersonate"),

    # Plans
    path("plans/", views.PlanListView.as_view(), name="plan-list"),
    path("plans/<int:pk>/", views.PlanDetailView.as_view(), name="plan-detail"),

    # User management
    path("users/", views.UserListView.as_view(), name="user-list"),
    path("users/<int:pk>/", views.UserDetailView.as_view(), name="user-detail"),
    path("users/<int:pk>/deactivate/", views.UserDeactivateView.as_view(), name="user-deactivate"),
    path("users/<int:pk>/activate/", views.UserActivateView.as_view(), name="user-activate"),

    # Payments & Revenue
    path("payments/", views.PaymentListView.as_view(), name="payment-list"),
    path("payments/summary/", views.PaymentSummaryView.as_view(), name="payment-summary"),

    # Analytics
    path("analytics/revenue-trend/", views.RevenueTrendView.as_view(), name="analytics-revenue"),
    path("analytics/tenant-growth/", views.TenantGrowthView.as_view(), name="analytics-growth"),
    path("analytics/churn/", views.ChurnView.as_view(), name="analytics-churn"),
    path("analytics/plan-distribution/", views.PlanDistributionView.as_view(), name="analytics-plans"),
    path("analytics/top-tenants/", views.TopTenantsView.as_view(), name="analytics-top"),

    # Audit log
    path("audit-log/", views.AuditLogView.as_view(), name="audit-log"),

    # Activity feed
    path("activity/", views.ActivityView.as_view(), name="activity"),

    # Settings
    path("settings/", views.SettingsView.as_view(), name="settings"),

    # Export
    path("export/tenants/", views.ExportTenantsView.as_view(), name="export-tenants"),
    path("export/users/", views.ExportUsersView.as_view(), name="export-users"),
    path("export/payments/", views.ExportPaymentsView.as_view(), name="export-payments"),

    # Changelog Management
    path("changelogs/", views.SuperadminChangelogListView.as_view(), name="superadmin-changelog-list"),
    path("changelogs/<int:pk>/", views.SuperadminChangelogDetailView.as_view(), name="superadmin-changelog-detail"),

    # Feature Request Management
    path("feature-requests/", views.SuperadminFeatureListView.as_view(), name="superadmin-feature-list"),
    path("feature-requests/<int:pk>/", views.SuperadminFeatureDetailView.as_view(), name="superadmin-feature-detail"),

    # Billing Cycles
    path("billing-cycles/", views.BillingCycleListView.as_view(), name="billing-cycle-list"),
    path("subscription-invoices/", views.SubscriptionInvoiceListView.as_view(), name="subscription-invoice-list"),
    path("subscription-invoices/<uuid:pk>/", views.SubscriptionInvoiceDetailView.as_view(), name="subscription-invoice-detail"),
    path("subscription-invoices/<uuid:pk>/send/", views.SubscriptionInvoiceSendView.as_view(), name="subscription-invoice-send"),

    # Tenant User Ledger (Immutable Audit Trail)
    path("user-ledger/", views.TenantUserLedgerListView.as_view(), name="user-ledger-list"),

    # Subscription Payments (Tuma STK Push)
    path("subscriptions/pay/", views.SubscriptionStkPushView.as_view(), name="subscription-stk-push"),

    # Subscription Payment History (platform billing records)
    path("subscription-payments/", views.SubscriptionPaymentListView.as_view(), name="subscription-payment-list"),

    # Leads
    path("leads/", views.LeadListView.as_view(), name="lead-list"),
    path("leads/stats/", views.LeadStatsView.as_view(), name="lead-stats"),
    path("leads/<int:pk>/", views.LeadDetailView.as_view(), name="lead-detail"),
    path("tenant-deletion-jobs/<uuid:job_id>/", views.TenantDeletionJobDetailView.as_view(), name="tenant-deletion-job-detail"),
]
