from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'departments', views.DepartmentViewSet)
router.register(r'employees', views.EmployeeViewSet)
router.register(r'attendance', views.AttendanceViewSet)
router.register(r'leave-requests', views.LeaveRequestViewSet)
router.register(r'performance-reviews', views.PerformanceReviewViewSet)
router.register(r'payroll', views.PayrollViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path('attendance-report/', views.AttendanceReportView.as_view(), name='attendance-report'),
    path('department-report/', views.DepartmentReportView.as_view(), name='department-report'),
]
