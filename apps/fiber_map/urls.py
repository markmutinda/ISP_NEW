from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import NetworkMapElementViewSet

router = DefaultRouter()
router.register(r'elements', NetworkMapElementViewSet, basename='network-map-element')

urlpatterns = [
    path('', include(router.urls)),
]