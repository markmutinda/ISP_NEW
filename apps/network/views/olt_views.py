# apps/network/views/olt_views.py
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from apps.network.models.olt_models import (
    OLTDevice, OLTPort, PONPort, ONUDevice, OLTConfig
)
from apps.network.serializers.olt_serializers import (
    OLTDeviceSerializer, OLTPortSerializer, PONPortSerializer,
    ONUDeviceSerializer, OLTConfigSerializer
)
from apps.core.permissions import HasCompanyAccess
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
import logging

logger = logging.getLogger(__name__)


class OLTDeviceViewSet(viewsets.ModelViewSet):
    queryset = OLTDevice.objects.all().select_related('company')
    serializer_class = OLTDeviceSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['vendor', 'status', 'company']
    search_fields = ['name', 'hostname', 'ip_address', 'serial_number', 'model']
    ordering_fields = ['name', 'created_at', 'last_sync']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return OLTDevice.objects.all()
        return OLTDevice.objects.filter(company__in=user.companies.all())
    
    @action(detail=True, methods=['post'])
    def sync(self, request, pk=None):
        """Sync OLT device information"""
        olt = self.get_object()
        try:
            # Here you would call the integration
            # For now, we'll just update the sync timestamp
            olt.last_sync = timezone.now()
            olt.save()
            
            # Call sync method from integration
            from apps.network.integrations.olt_integration import OLTManager
            olt_manager = OLTManager(olt)
            result = olt_manager.sync_device_info()
            
            return Response({
                'status': 'success',
                'message': f'OLT {olt.name} synced successfully',
                'data': result
            })
        except Exception as e:
            logger.error(f"Failed to sync OLT {olt.name}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['get'])
    def ports(self, request, pk=None):
        """Get all ports for this OLT"""
        olt = self.get_object()
        ports = olt.ports.all()
        serializer = OLTPortSerializer(ports, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def stats(self, request, pk=None):
        """Get OLT statistics"""
        olt = self.get_object()
        stats = {
            'total_ports': olt.ports.count(),
            'active_ports': olt.ports.filter(operational_state=True).count(),
            'total_pon_ports': PONPort.objects.filter(olt_port__olt=olt).count(),
            'total_onus': ONUDevice.objects.filter(pon_port__olt_port__olt=olt).count(),
            'online_onus': ONUDevice.objects.filter(
                pon_port__olt_port__olt=olt,
                status__in=['ONLINE', 'REGISTERED']
            ).count(),
            'last_sync': olt.last_sync,
        }
        return Response(stats)

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return queryset.filter(company_id=company_id)
            return queryset
        
        if hasattr(user, 'company') and user.company:
            return queryset.filter(company=user.company)
        
        return queryset.none()

class OLTPortViewSet(viewsets.ModelViewSet):
    queryset = OLTPort.objects.all().select_related('olt', 'olt__company')
    serializer_class = OLTPortSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['olt', 'port_type', 'admin_state', 'operational_state']
    search_fields = ['port_number', 'description']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return OLTPort.objects.all()
        return OLTPort.objects.filter(olt__company__in=user.companies.all())
    
    @action(detail=True, methods=['post'])
    def toggle_state(self, request, pk=None):
        """Toggle port admin state"""
        port = self.get_object()
        port.admin_state = not port.admin_state
        port.save()
        
        # Here you would call the integration to apply changes to device
        return Response({
            'status': 'success',
            'message': f'Port {port.port_number} state toggled',
            'admin_state': port.admin_state
        })

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return queryset.filter(olt__company_id=company_id)
            return queryset
        
        if hasattr(user, 'company') and user.company:
            return queryset.filter(olt__company=user.company)
        
        return queryset.none()

class PONPortViewSet(viewsets.ModelViewSet):
    queryset = PONPort.objects.all().select_related(
        'olt_port', 'olt_port__olt', 'olt_port__olt__company'
    )
    serializer_class = PONPortSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['olt_port', 'pon_type', 'status']
    search_fields = ['pon_index']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return PONPort.objects.all()
        return PONPort.objects.filter(olt_port__olt__company__in=user.companies.all())
    
    @action(detail=True, methods=['get'])
    def onus(self, request, pk=None):
        """Get all ONUs on this PON port"""
        pon_port = self.get_object()
        onus = pon_port.onus.all()
        serializer = ONUDeviceSerializer(onus, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def performance(self, request, pk=None):
        """Get PON port performance metrics"""
        pon_port = self.get_object()
        # Calculate average signal levels
        onus = pon_port.onus.all()
        avg_rx = onus.aggregate(Avg('rx_power'))['rx_power__avg']
        avg_tx = onus.aggregate(Avg('tx_power'))['tx_power__avg']
        
        return Response({
            'total_onus': pon_port.total_onus,
            'registered_onus': pon_port.registered_onus,
            'registration_rate': (pon_port.registered_onus / pon_port.total_onus * 100) if pon_port.total_onus > 0 else 0,
            'avg_rx_power': avg_rx,
            'avg_tx_power': avg_tx,
        })

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return queryset.filter(olt_port__olt__company_id=company_id)
            return queryset
        
        if hasattr(user, 'company') and user.company:
            return queryset.filter(olt_port__olt__company=user.company)
        
        return queryset.none()

class ONUDeviceViewSet(viewsets.ModelViewSet):
    queryset = ONUDevice.objects.all().select_related(
        'pon_port', 'pon_port__olt_port', 'pon_port__olt_port__olt',
        'service_connection', 'service_connection__customer'
    )
    serializer_class = ONUDeviceSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['pon_port', 'onu_type', 'status']
    search_fields = ['serial_number', 'mac_address', 'onu_index']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return ONUDevice.objects.all()
        return ONUDevice.objects.filter(
            pon_port__olt_port__olt__company__in=user.companies.all()
        )
    
    @action(detail=True, methods=['post'])
    def sync(self, request, pk=None):
        """Sync ONU information from OLT"""
        onu = self.get_object()
        try:
            # Call integration to sync ONU info
            from apps.network.integrations.olt_integration import OLTManager
            olt_manager = OLTManager(onu.pon_port.olt_port.olt)
            result = olt_manager.get_onu_info(onu.serial_number)
            
            # Update ONU with synced data
            if result:
                onu.rx_power = result.get('rx_power')
                onu.tx_power = result.get('tx_power')
                onu.distance = result.get('distance')
                onu.status = result.get('status', onu.status)
                onu.last_seen = timezone.now()
                onu.save()
            
            return Response({
                'status': 'success',
                'message': f'ONU {onu.serial_number} synced',
                'data': result
            })
        except Exception as e:
            logger.error(f"Failed to sync ONU {onu.serial_number}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['post'])
    def reboot(self, request, pk=None):
        """Reboot ONU device"""
        onu = self.get_object()
        try:
            # Call integration to reboot ONU
            from apps.network.integrations.olt_integration import OLTManager
            olt_manager = OLTManager(onu.pon_port.olt_port.olt)
            result = olt_manager.reboot_onu(onu.serial_number)
            
            return Response({
                'status': 'success',
                'message': f'ONU {onu.serial_number} reboot command sent',
                'data': result
            })
        except Exception as e:
            logger.error(f"Failed to reboot ONU {onu.serial_number}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return queryset.filter(pon_port__olt_port__olt__company_id=company_id)
            return queryset
        
        if hasattr(user, 'company') and user.company:
            return queryset.filter(pon_port__olt_port__olt__company=user.company)
        
        return queryset.none()

class OLTConfigViewSet(viewsets.ModelViewSet):
    queryset = OLTConfig.objects.all().select_related('olt', 'olt__company', 'applied_by')
    serializer_class = OLTConfigSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['olt', 'config_type', 'is_active']
    search_fields = ['version']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return OLTConfig.objects.all()
        return OLTConfig.objects.filter(olt__company__in=user.companies.all())
    
    @action(detail=True, methods=['post'])
    def apply(self, request, pk=None):
        """Apply this configuration to OLT"""
        config = self.get_object()
        try:
            # Call integration to apply configuration
            from apps.network.integrations.olt_integration import OLTManager
            olt_manager = OLTManager(config.olt)
            result = olt_manager.apply_config(config.config_data)
            
            # Create a new config entry
            new_config = OLTConfig.objects.create(
                olt=config.olt,
                config_type='RUNNING',
                version=f"v{config.version}",
                config_data=config.config_data,
                checksum=config.checksum,
                applied_by=request.user,
                is_active=True
            )
            
            # Deactivate old configs
            OLTConfig.objects.filter(
                olt=config.olt,
                config_type='RUNNING'
            ).exclude(id=new_config.id).update(is_active=False)
            
            return Response({
                'status': 'success',
                'message': f'Configuration applied to {config.olt.name}',
                'config_id': new_config.id
            })
        except Exception as e:
            logger.error(f"Failed to apply config to {config.olt.name}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['get'])
    def compare(self, request, pk=None):
        """Compare this config with active config"""
        config = self.get_object()
        active_config = OLTConfig.objects.filter(
            olt=config.olt,
            config_type='RUNNING',
            is_active=True
        ).first()
        
        if not active_config:
            return Response({
                'status': 'error',
                'message': 'No active configuration found'
            })
        
        # Simple diff (for demo - use proper diff in production)
        diff = {
            'current_version': active_config.version,
            'new_version': config.version,
            'lines_changed': 'N/A',  # Implement proper diff here
            'summary': 'Configurations can be compared'
        }
        
        return Response(diff)
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return queryset.filter(olt__company_id=company_id)
            return queryset
        
        if hasattr(user, 'company') and user.company:
            return queryset.filter(olt__company=user.company)
        
        return queryset.none()