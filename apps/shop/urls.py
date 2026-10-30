from django.urls import include, path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("admin/categories", views.AdminCategoryViewSet, basename="shop-admin-category")
router.register("admin/products", views.AdminProductViewSet, basename="shop-admin-product")
router.register("admin/orders", views.AdminOrderViewSet, basename="shop-admin-order")

urlpatterns = [
    path("settings/", views.ShopSettingsView.as_view()),
    path("admin/settings/", views.AdminShopSettingsView.as_view()),
    path("categories/", views.CategoryListView.as_view()),
    path("products/", views.ProductListView.as_view()),
    path("products/<slug:slug>/", views.ProductDetailView.as_view()),
    path("products/<slug:slug>/related/", views.RelatedProductsView.as_view()),
    path("checkout/", views.CheckoutView.as_view()),
    path("orders/<str:reference>/", views.OrderStatusView.as_view()),
    path("orders/<str:reference>/pay/", views.OrderPayView.as_view()),
    path("webhooks/pay/<str:secret>/", views.PaymentCallbackView.as_view()),
    path("", include(router.urls)),
]
