from django.urls import path
from . import views

urlpatterns = [
    path('reports/', views.ReportGeneratorView.as_view(), name='reports'),
    path('reports/generate/', views.ReportGeneratorView.as_view(), name='generate-report'),
    path('dashboard/', views.DashboardView.as_view(), name='dashboard'),
    path('export/', views.ExportView.as_view(), name='export-data'),
]