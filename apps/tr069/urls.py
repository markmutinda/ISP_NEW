from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r'devices', views.CPEDeviceViewSet, basename='cpe-device')

urlpatterns = [
    path('', include(router.urls)),
    path('pppoe-subscribers/', views.PppoeSubscribersView.as_view(), name='tr069-pppoe-subscribers'),
]