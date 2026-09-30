import logging

from django.db.models import Count, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import HasRoleAccessPolicy

from .models import CPEDevice
from .serializers import (
    CPEDeviceCreateSerializer, CPEDeviceDetailSerializer, CPEDeviceListSerializer,
    CPEDevicePatchSerializer, CPETaskLogSerializer, CredentialsSerializer,
    FactoryResetSerializer, PppoeSubscriberSerializer, SetWifiSerializer,
)
from .services import device_credentials, provisioning

logger = logging.getLogger(__name__)

_ERROR_STATUS = {
    'duplicate': status.HTTP_409_CONFLICT,
    'claimed': status.HTTP_409_CONFLICT,
    'not_seen': status.HTTP_409_CONFLICT,
    'not_ready': status.HTTP_409_CONFLICT,
    'unsupported': status.HTTP_409_CONFLICT,
    'acs_error': status.HTTP_502_BAD_GATEWAY,
}


def _error_response(exc: provisioning.ProvisioningError):
    return Response(
        {'error': str(exc), 'code': exc.code},
        status=_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
    )


class CPEDeviceViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/tr069"
    lookup_field = 'pk'

    def get_queryset(self):
        qs = CPEDevice.objects.select_related('customer', 'service_connection').annotate(
            pending_commands=Count('task_logs', filter=Q(task_logs__status='queued'))
        )
        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(label__icontains=search) | Q(serial_number__icontains=search)
                | Q(model_name__icontains=search)
            )
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        if self.request.query_params.get('weak_signal') == 'true':
            qs = qs.filter(weak_signal=True)
        subscriber = self.request.query_params.get('subscriber')
        if subscriber:
            qs = qs.filter(service_connection_id=subscriber)
        return qs

    def get_serializer_class(self):
        if self.action == 'list':
            return CPEDeviceListSerializer
        return CPEDeviceDetailSerializer

    def create(self, request, *args, **kwargs):
        serializer = CPEDeviceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        service_connection = None
        customer = None
        sc_id = data.get('service_connection')
        if sc_id:
            from apps.customers.models import ServiceConnection
            service_connection = ServiceConnection.objects.filter(pk=sc_id).select_related('customer').first()
            if not service_connection:
                return Response({'service_connection': ['Not found.']}, status=400)
            customer = service_connection.customer

        try:
            device = provisioning.enroll_device(
                serial_number=data['serial_number'],
                label=data.get('label', ''),
                customer=customer,
                service_connection=service_connection,
            )
        except provisioning.ProvisioningError as exc:
            return _error_response(exc)

        device = CPEDevice.objects.annotate(
            pending_commands=Count('task_logs', filter=Q(task_logs__status='queued'))
        ).get(pk=device.pk)
        return Response(CPEDeviceDetailSerializer(device).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        device = self.get_object()
        serializer = CPEDevicePatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if 'label' in data:
            device.label = data['label']
        if 'service_connection' in data:
            sc_id = data['service_connection']
            if sc_id:
                from apps.customers.models import ServiceConnection
                sc = ServiceConnection.objects.filter(pk=sc_id).select_related('customer').first()
                if not sc:
                    return Response({'service_connection': ['Not found.']}, status=400)
                device.service_connection = sc
                device.customer = sc.customer
            else:
                device.service_connection = None
                device.customer = None
        device.save()
        return Response(CPEDeviceDetailSerializer(device).data)

    def destroy(self, request, *args, **kwargs):
        device = self.get_object()
        provisioning.remove_device(device)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['get'])
    def summary(self, request):
        qs = self.get_queryset()
        return Response({
            'total': qs.count(),
            'online': qs.filter(status='online').count(),
            'not_answering': qs.filter(status='not_answering').count(),
            'never_seen': qs.filter(status='never_seen').count(),
            'weak_signal': qs.filter(weak_signal=True).count(),
            'pending_commands': sum(qs.values_list('pending_commands', flat=True)),
        })

    @action(detail=True, methods=['get'])
    def credentials(self, request, pk=None):
        device = self.get_object()
        data = {
            'acs_url': device_credentials.acs_url(),
            'username': device.acs_username,
            'password': device.acs_password,
            'recommended_interval_seconds': 300,
        }
        return Response(CredentialsSerializer(data).data)

    @action(detail=True, methods=['post'])
    def rotate_credentials(self, request, pk=None):
        device = self.get_object()
        device = provisioning.rotate_credentials(device)
        data = {
            'acs_url': device_credentials.acs_url(),
            'username': device.acs_username,
            'password': device.acs_password,
            'recommended_interval_seconds': 300,
        }
        return Response(CredentialsSerializer(data).data)

    @action(detail=True, methods=['post'])
    def refresh(self, request, pk=None):
        device = self.get_object()
        try:
            log = provisioning.refresh_device(device, user=request.user)
        except provisioning.ProvisioningError as exc:
            return _error_response(exc)
        http_status = status.HTTP_200_OK if log.status == 'completed' else status.HTTP_202_ACCEPTED
        return Response({'status': log.status, 'task': CPETaskLogSerializer(log).data}, status=http_status)

    @action(detail=True, methods=['post'])
    def reboot(self, request, pk=None):
        device = self.get_object()
        try:
            log = provisioning.reboot_device(device, user=request.user)
        except provisioning.ProvisioningError as exc:
            return _error_response(exc)
        http_status = status.HTTP_200_OK if log.status == 'completed' else status.HTTP_202_ACCEPTED
        return Response({'status': log.status, 'task': CPETaskLogSerializer(log).data}, status=http_status)

    @action(detail=True, methods=['post'])
    def factory_reset(self, request, pk=None):
        device = self.get_object()
        serializer = FactoryResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            log = provisioning.factory_reset_device(device, user=request.user)
        except provisioning.ProvisioningError as exc:
            return _error_response(exc)
        http_status = status.HTTP_200_OK if log.status == 'completed' else status.HTTP_202_ACCEPTED
        return Response({'status': log.status, 'task': CPETaskLogSerializer(log).data}, status=http_status)

    @action(detail=True, methods=['post'])
    def set_wifi(self, request, pk=None):
        device = self.get_object()
        serializer = SetWifiSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            log = provisioning.set_wifi(device, serializer.validated_data['networks'], user=request.user)
        except provisioning.ProvisioningError as exc:
            return _error_response(exc)
        http_status = status.HTTP_200_OK if log.status == 'completed' else status.HTTP_202_ACCEPTED
        return Response({'status': log.status, 'task': CPETaskLogSerializer(log).data}, status=http_status)

    @action(detail=True, methods=['get'])
    def tasks(self, request, pk=None):
        device = self.get_object()
        logs = device.task_logs.all()[:50]
        return Response(CPETaskLogSerializer(logs, many=True).data)


class PppoeSubscribersView(APIView):
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/tr069"

    def get(self, request):
        from apps.customers.models import ServiceConnection

        search = request.query_params.get('search', '').strip()
        qs = ServiceConnection.objects.select_related('customer', 'plan').filter(status='ACTIVE')
        if search:
            qs = qs.filter(customer__full_name__icontains=search)
        qs = qs[:25]

        linked_ids = set(CPEDevice.objects.exclude(service_connection__isnull=True).values_list('service_connection_id', flat=True))

        results = [{
            'id': sc.id,
            'name': getattr(sc.customer, 'full_name', str(sc.customer)),
            'plan': getattr(sc.plan, 'name', None),
            'already_linked': sc.id in linked_ids,
        } for sc in qs]
        return Response(PppoeSubscriberSerializer(results, many=True).data)