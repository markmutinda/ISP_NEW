# apps/network/views/mikrotik_views.py
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from apps.network.models.mikrotik_models import (
    MikrotikDevice, MikrotikInterface, HotspotUser,
    PPPoEUser, MikrotikQueue
)
from apps.network.serializers.mikrotik_serializers import (
    MikrotikDeviceSerializer, MikrotikInterfaceSerializer,
    HotspotUserSerializer, PPPoEUserSerializer, MikrotikQueueSerializer
)
from apps.core.permissions import HasCompanyAccess
import logging

logger = logging.getLogger(__name__)


class MikrotikDeviceViewSet(viewsets.ModelViewSet):
    queryset = MikrotikDevice.objects.all().select_related('company')
    serializer_class = MikrotikDeviceSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['company', 'device_type', 'status']
    search_fields = ['name', 'hostname', 'ip_address', 'serial_number', 'model']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return MikrotikDevice.objects.all()
        return MikrotikDevice.objects.filter(company__in=user.companies.all())
    
    @action(detail=True, methods=['post'])
    def sync(self, request, pk=None):
        """Sync Mikrotik device information"""
        device = self.get_object()
        try:
            from apps.network.integrations.mikrotik_api import MikrotikAPI
            api = MikrotikAPI(device)
            result = api.sync_device_info()
            
            # Update device info
            device.cpu_load = result.get('cpu_load')
            device.memory_usage = result.get('memory_usage')
            device.disk_usage = result.get('disk_usage')
            device.uptime = result.get('uptime')
            device.last_sync = timezone.now()
            device.save()
            
            # Sync interfaces
            interfaces = result.get('interfaces', [])
            for iface_data in interfaces:
                MikrotikInterface.objects.update_or_create(
                    mikrotik=device,
                    interface_name=iface_data['name'],
                    defaults={
                        'interface_type': iface_data.get('type', 'ETHERNET'),
                        'mac_address': iface_data.get('mac_address', ''),
                        'mtu': iface_data.get('mtu', 1500),
                        'rx_bytes': iface_data.get('rx_bytes', 0),
                        'tx_bytes': iface_data.get('tx_bytes', 0),
                        'admin_state': iface_data.get('admin_state', True),
                        'operational_state': iface_data.get('operational_state', False),
                    }
                )
            
            return Response({
                'status': 'success',
                'message': f'Mikrotik {device.name} synced successfully',
                'data': {
                    'interfaces_synced': len(interfaces),
                    'cpu_load': device.cpu_load,
                    'memory_usage': device.memory_usage,
                }
            })
        except Exception as e:
            logger.error(f"Failed to sync Mikrotik {device.name}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['get'])
    def interfaces(self, request, pk=None):
        """Get all interfaces for this device"""
        device = self.get_object()
        interfaces = device.interfaces.all()
        serializer = MikrotikInterfaceSerializer(interfaces, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def hotspot_users(self, request, pk=None):
        """Get all hotspot users for this device"""
        device = self.get_object()
        users = device.hotspot_users.all()
        serializer = HotspotUserSerializer(users, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def pppoe_users(self, request, pk=None):
        """Get all PPPoE users for this device"""
        device = self.get_object()
        users = device.pppoe_users.all()
        serializer = PPPoEUserSerializer(users, many=True)
        return Response(serializer.data)


class MikrotikInterfaceViewSet(viewsets.ModelViewSet):
    queryset = MikrotikInterface.objects.all().select_related('mikrotik', 'mikrotik__company')
    serializer_class = MikrotikInterfaceSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['mikrotik', 'interface_type', 'admin_state', 'operational_state']
    search_fields = ['interface_name', 'mac_address']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return MikrotikInterface.objects.all()
        return MikrotikInterface.objects.filter(mikrotik__company__in=user.companies.all())
    
    @action(detail=True, methods=['post'])
    def toggle(self, request, pk=None):
        """Toggle interface state"""
        interface = self.get_object()
        try:
            from apps.network.integrations.mikrotik_api import MikrotikAPI
            api = MikrotikAPI(interface.mikrotik)
            
            if interface.admin_state:
                result = api.disable_interface(interface.interface_name)
                interface.admin_state = False
            else:
                result = api.enable_interface(interface.interface_name)
                interface.admin_state = True
            
            interface.save()
            
            return Response({
                'status': 'success',
                'message': f'Interface {interface.interface_name} toggled',
                'admin_state': interface.admin_state,
                'data': result
            })
        except Exception as e:
            logger.error(f"Failed to toggle interface {interface.interface_name}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)


class HotspotUserViewSet(viewsets.ModelViewSet):
    queryset = HotspotUser.objects.all().select_related(
        'mikrotik', 'mikrotik__company',
        'service_connection', 'service_connection__customer'
    )
    serializer_class = HotspotUserSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['mikrotik', 'status', 'profile']
    search_fields = ['username', 'mac_address', 'ip_address']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return HotspotUser.objects.all()
        return HotspotUser.objects.filter(mikrotik__company__in=user.companies.all())
    
    @action(detail=True, methods=['post'])
    def enable(self, request, pk=None):
        """Enable hotspot user"""
        user = self.get_object()
        try:
            from apps.network.integrations.mikrotik_api import MikrotikAPI
            api = MikrotikAPI(user.mikrotik)
            result = api.enable_hotspot_user(user.username)
            
            user.status = 'ACTIVE'
            user.save()
            
            return Response({
                'status': 'success',
                'message': f'Hotspot user {user.username} enabled',
                'data': result
            })
        except Exception as e:
            logger.error(f"Failed to enable hotspot user {user.username}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        """Disable hotspot user"""
        user = self.get_object()
        try:
            from apps.network.integrations.mikrotik_api import MikrotikAPI
            api = MikrotikAPI(user.mikrotik)
            result = api.disable_hotspot_user(user.username)
            
            user.status = 'DISABLED'
            user.save()
            
            return Response({
                'status': 'success',
                'message': f'Hotspot user {user.username} disabled',
                'data': result
            })
        except Exception as e:
            logger.error(f"Failed to disable hotspot user {user.username}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['post'])
    def sync(self, request, pk=None):
        """Sync hotspot user stats"""
        user = self.get_object()
        try:
            from apps.network.integrations.mikrotik_api import MikrotikAPI
            api = MikrotikAPI(user.mikrotik)
            stats = api.get_hotspot_user_stats(user.username)
            
            if stats:
                user.ip_address = stats.get('address')
                user.bytes_in = stats.get('bytes_in', 0)
                user.bytes_out = stats.get('bytes_out', 0)
                user.session_time = stats.get('session_time')
                user.idle_time = stats.get('idle_time')
                user.last_login = stats.get('last_login')
                user.last_logout = stats.get('last_logout')
                user.save()
            
            return Response({
                'status': 'success',
                'message': f'Hotspot user {user.username} synced',
                'data': stats
            })
        except Exception as e:
            logger.error(f"Failed to sync hotspot user {user.username}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)


class PPPoEUserViewSet(viewsets.ModelViewSet):
    queryset = PPPoEUser.objects.all().select_related(
        'mikrotik', 'mikrotik__company',
        'service_connection', 'service_connection__customer'
    )
    serializer_class = PPPoEUserSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['mikrotik', 'status', 'profile']
    search_fields = ['username', 'caller_id', 'local_address', 'remote_address']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return PPPoEUser.objects.all()
        return PPPoEUser.objects.filter(mikrotik__company__in=user.companies.all())
    
    @action(detail=True, methods=['post'])
    def sync(self, request, pk=None):
        """Sync PPPoE user stats"""
        pppoe_user = self.get_object()
        try:
            from apps.network.integrations.mikrotik_api import MikrotikAPI
            api = MikrotikAPI(pppoe_user.mikrotik)
            stats = api.get_pppoe_user_stats(pppoe_user.username)
            
            if stats:
                pppoe_user.local_address = stats.get('local_address')
                pppoe_user.remote_address = stats.get('remote_address')
                pppoe_user.bytes_in = stats.get('bytes_in', 0)
                pppoe_user.bytes_out = stats.get('bytes_out', 0)
                pppoe_user.session_time = stats.get('session_time')
                pppoe_user.status = 'CONNECTED' if stats.get('connected') else 'DISCONNECTED'
                pppoe_user.last_connection = stats.get('last_connection')
                pppoe_user.save()
            
            return Response({
                'status': 'success',
                'message': f'PPPoE user {pppoe_user.username} synced',
                'data': stats
            })
        except Exception as e:
            logger.error(f"Failed to sync PPPoE user {pppoe_user.username}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)


class MikrotikQueueViewSet(viewsets.ModelViewSet):
    queryset = MikrotikQueue.objects.all().select_related(
        'mikrotik', 'mikrotik__company',
        'hotspot_user', 'pppoe_user'
    )
    serializer_class = MikrotikQueueSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['mikrotik', 'queue_type', 'disabled']
    search_fields = ['queue_name', 'target']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return MikrotikQueue.objects.all()
        return MikrotikQueue.objects.filter(mikrotik__company__in=user.companies.all())
    
    @action(detail=True, methods=['post'])
    def apply(self, request, pk=None):
        """Apply queue to Mikrotik device"""
        queue = self.get_object()
        try:
            from apps.network.integrations.mikrotik_api import MikrotikAPI
            api = MikrotikAPI(queue.mikrotik)
            result = api.create_queue(
                name=queue.queue_name,
                target=queue.target,
                max_limit=queue.max_limit,
                burst_limit=queue.burst_limit,
                priority=queue.priority
            )
            
            return Response({
                'status': 'success',
                'message': f'Queue {queue.queue_name} applied to {queue.mikrotik.name}',
                'data': result
            })
        except Exception as e:
            logger.error(f"Failed to apply queue {queue.queue_name}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['post'])
    def toggle(self, request, pk=None):
        """Toggle queue state"""
        queue = self.get_object()
        try:
            from apps.network.integrations.mikrotik_api import MikrotikAPI
            api = MikrotikAPI(queue.mikrotik)
            
            if queue.disabled:
                result = api.enable_queue(queue.queue_name)
                queue.disabled = False
            else:
                result = api.disable_queue(queue.queue_name)
                queue.disabled = True
            
            queue.save()
            
            return Response({
                'status': 'success',
                'message': f'Queue {queue.queue_name} toggled',
                'disabled': queue.disabled,
                'data': result
            })
        except Exception as e:
            logger.error(f"Failed to toggle queue {queue.queue_name}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)