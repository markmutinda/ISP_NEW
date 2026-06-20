from django.urls import path

from . import support_views

app_name = "support_console"

urlpatterns = [
    path("login/", support_views.SupportConsoleLoginView.as_view(), name="login"),
    path("me/", support_views.SupportConsoleMeView.as_view(), name="me"),
    path("dashboard/", support_views.SupportConsoleDashboardView.as_view(), name="dashboard"),
    path("activity/", support_views.SupportConsoleActivityView.as_view(), name="activity"),
    path("leads/", support_views.SupportConsoleLeadListView.as_view(), name="leads"),
    path("leads/<int:pk>/", support_views.SupportConsoleLeadDetailView.as_view(), name="lead-detail"),
]
