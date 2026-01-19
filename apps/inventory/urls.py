from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

router = DefaultRouter()
router.register(r'suppliers', views.SupplierViewSet)
router.register(r'equipment-types', views.EquipmentTypeViewSet)
router.register(r'equipment', views.EquipmentItemViewSet)
router.register(r'assignments', views.AssignmentViewSet)
router.register(r'purchase-orders', views.PurchaseOrderViewSet)
router.register(r'purchase-order-items', views.PurchaseOrderItemViewSet)
router.register(r'maintenance', views.MaintenanceRecordViewSet)
router.register(r'stock-alerts', views.StockAlertViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path('stock-report/', views.StockReportView.as_view(), name='stock-report'),
    path('check-stock/', views.StockAlertViewSet.as_view({'get': 'check_stock'}), name='check-stock'),
]
