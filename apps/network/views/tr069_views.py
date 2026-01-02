# apps/network/views/tr069_views.py
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from apps.network.models.tr069_models import (
    ACSConfiguration, CPEDevice, TR069Parameter, TR069Session
)
from apps.network.serializers.tr069_serializers import (
    ACSConfigurationSerializer, CPEDeviceSerializer,
    TR069ParameterSerializer, TR069SessionSerializer
)
from apps.core.permissions import HasCompanyAccess
import logging

logger = logging.getLogger(__name__)


class ACSConfigurationViewSet(viewsets.ModelViewSet):
    queryset = ACSConfiguration.objects.all().select_related('company')
    serializer_class = ACSConfigurationSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['company', 'is_active']
    search_fields = ['name', 'acs_url']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return ACSConfiguration.objects.all()
        return ACSConfiguration.objects.filter(company__in=user.companies.all())


class CPEDeviceViewSet(viewsets.ModelViewSet):
    queryset = CPEDevice.objects.all().select_related(
        'company', 'service_connection', 'service_connection__customer',
        'acs_config'
    )
    serializer_class = CPEDeviceSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['company', 'manufacturer', 'connection_status', 'provisioned']
    search_fields = ['serial_number', 'model', 'cpe_id', 'wan_ip']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return CPEDevice.objects.all()
        return CPEDevice.objects.filter(company__in=user.companies.all())
    
    @action(detail=True, methods=['post'])
    def provision(self, request, pk=None):
        """Provision CPE device"""
        cpe = self.get_object()
        try:
            # Call TR-069 client to provision device
            from apps.network.integrations.tr069_client import TR069Client
            client = TR069Client(cpe.acs_config)
            result = client.provision_device(cpe)
            
            cpe.provisioned = True
            cpe.configuration_file = result.get('config_file', '')
            cpe.save()
            
            return Response({
                'status': 'success',
                'message': f'CPE {cpe.serial_number} provisioned',
                'data': result
            })
        except Exception as e:
            logger.error(f"Failed to provision CPE {cpe.serial_number}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['post'])
    def reboot(self, request, pk=None):
        """Reboot CPE device"""
        cpe = self.get_object()
        try:
            from apps.network.integrations.tr069_client import TR069Client
            client = TR069Client(cpe.acs_config)
            result = client.reboot_device(cpe)
            
            return Response({
                'status': 'success',
                'message': f'Reboot command sent to {cpe.serial_number}',
                'data': result
            })
        except Exception as e:
            logger.error(f"Failed to reboot CPE {cpe.serial_number}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['get'])
    def parameters(self, request, pk=None):
        """Get all parameters for CPE device"""
        cpe = self.get_object()
        parameters = cpe.parameters.all()
        serializer = TR069ParameterSerializer(parameters, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def sync_parameters(self, request, pk=None):
        """Sync parameters from CPE device"""
        cpe = self.get_object()
        try:
            from apps.network.integrations.tr069_client import TR069Client
            client = TR069Client(cpe.acs_config)
            parameters = client.get_parameter_values(cpe)
            
            # Update or create parameters
            for param_name, param_value in parameters.items():
                TR069Parameter.objects.update_or_create(
                    cpe_device=cpe,
                    parameter_name=param_name,
                    defaults={
                        'current_value': str(param_value),
                        'last_updated': timezone.now()
                    }
                )
            
            return Response({
                'status': 'success',
                'message': f'Synced {len(parameters)} parameters from {cpe.serial_number}',
                'count': len(parameters)
            })
        except Exception as e:
            logger.error(f"Failed to sync parameters for CPE {cpe.serial_number}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['get'])
    def sessions(self, request, pk=None):
        """Get session history for CPE device"""
        cpe = self.get_object()
        sessions = cpe.sessions.all().order_by('-start_time')
        serializer = TR069SessionSerializer(sessions, many=True)
        return Response(serializer.data)


class TR069ParameterViewSet(viewsets.ModelViewSet):
    queryset = TR069Parameter.objects.all().select_related('cpe_device', 'cpe_device__company')
    serializer_class = TR069ParameterSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['cpe_device', 'parameter_type', 'access_type']
    search_fields = ['parameter_name']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return TR069Parameter.objects.all()
        return TR069Parameter.objects.filter(cpe_device__company__in=user.companies.all())
    
    @action(detail=True, methods=['post'])
    def set_value(self, request, pk=None):
        """Set parameter value on CPE device"""
        parameter = self.get_object()
        value = request.data.get('value')
        
        if not value:
            return Response({
                'status': 'error',
                'message': 'Value is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            from apps.network.integrations.tr069_client import TR069Client
            client = TR069Client(parameter.cpe_device.acs_config)
            result = client.set_parameter_value(
                parameter.cpe_device,
                parameter.parameter_name,
                value
            )
            
            parameter.configured_value = value
            parameter.save()
            
            return Response({
                'status': 'success',
                'message': f'Parameter {parameter.parameter_name} set to {value}',
                'data': result
            })
        except Exception as e:
            logger.error(f"Failed to set parameter {parameter.parameter_name}: {str(e)}")
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_400_BAD_REQUEST)


class TR069SessionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TR069Session.objects.all().select_related(
        'cpe_device', 'cpe_device__company', 'initiated_by'
    )
    serializer_class = TR069SessionSerializer
    permission_classes = [HasCompanyAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['cpe_device', 'session_type', 'status']
    search_fields = ['session_id', 'error_message']
    ordering_fields = ['start_time', 'end_time']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return TR069Session.objects.all()
        return TR069Session.objects.filter(cpe_device__company__in=user.companies.all())