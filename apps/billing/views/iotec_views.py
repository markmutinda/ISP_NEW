# apps/billing/views/iotec_views.py
import logging
from decimal import Decimal

from django.db import connection
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.permissions import HasRoleAccessPolicy, IsCompanyAdmin
from apps.core.country_utils import get_tenant_country_code
from ..models.payment_models import IotecConfiguration, InvoiceItemPayment
from ..serializers.payment_serializers import IotecConfigurationSerializer
from ..services import iotec_service

logger = logging.getLogger(__name__)


class IotecConfigurationViewSet(viewsets.ModelViewSet):
    """Tenant iotec credentials (Uganda). Secrets are write-only and encrypted at rest."""
    serializer_class = IotecConfigurationSerializer
    permission_classes = [IsAuthenticated, IsCompanyAdmin, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payment-methods"
    pagination_class = None
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']

    def get_queryset(self):
        return IotecConfiguration.objects.filter(
            schema_name=connection.schema_name
        ).order_by('-updated_at', '-id')

    # ── helpers ──
    @staticmethod
    def _require_uganda():
        if get_tenant_country_code(connection.schema_name) != 'UG':
            raise ValidationError("iotec Mobile Money is only available for Uganda tenants.")

    def _sync_payment_method(self, config):
        """Ensure one InvoiceItemPayment linked to this config; make it the sole active method."""
        schema = connection.schema_name
        method = InvoiceItemPayment.objects.filter(
            schema_name=schema, iotec_configuration=config
        ).first()
        if not method:
            method = InvoiceItemPayment.objects.create(
                schema_name=schema,
                iotec_configuration=config,
                name='iotec Mobile Money (MTN & Airtel)',
                code=f'IOTEC_{schema}_{config.pk}'[:100],   # code is globally unique
                method_type='IOTEC_MOMO',
                is_active=True,
                is_default=True,
                minimum_amount=Decimal('500'),
                maximum_amount=Decimal('10000000'),
                created_by=self.request.user,
            )
        InvoiceItemPayment.objects.filter(schema_name=schema).exclude(
            pk=method.pk
        ).update(is_active=False, is_default=False)
        method.is_active = True
        method.is_default = True
        method.status = 'ACTIVE'
        method.save(update_fields=['is_active', 'is_default', 'status', 'updated_at'])
        return method

    # ── CRUD ──
    def perform_create(self, serializer):
        self._require_uganda()
        config = serializer.save(created_by=self.request.user, schema_name=connection.schema_name)
        if config.is_active:
            self._sync_payment_method(config)

    def perform_update(self, serializer):
        config = serializer.save(updated_by=self.request.user)
        if config.is_active:
            self._sync_payment_method(config)

    def perform_destroy(self, instance):
        InvoiceItemPayment.objects.filter(iotec_configuration=instance).delete()  # Payment FK is SET_NULL
        instance.delete()

    # ── actions ──
    @action(detail=True, methods=['post'], url_path='test-connection')
    def test_connection(self, request, pk=None):
        config = self.get_object()
        result = iotec_service.test_connection(config)
        IotecConfiguration.objects.filter(pk=config.pk).update(   # .update(): don't bust token cache
            last_validated_at=timezone.now(),
            validation_status='VALID' if result['success'] else 'INVALID',
            validation_error='' if result['success'] else result['message'][:500],
        )
        return Response(result, status=status.HTTP_200_OK if result['success'] else status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'], url_path='activate-as-primary')
    def activate_as_primary(self, request, pk=None):
        config = self.get_object()
        self._require_uganda()
        config.is_active = True
        config.save()                     # model.save deactivates other iotec configs
        method = self._sync_payment_method(config)
        return Response({'status': 'success', 'message': 'iotec activated as the payment gateway.',
                         'payment_method_id': method.id, 'gateway': 'iotec'})

    @action(detail=True, methods=['post'], url_path='toggle-active')
    def toggle_active(self, request, pk=None):
        config = self.get_object()
        if config.is_active:
            IotecConfiguration.objects.filter(pk=config.pk).update(is_active=False)
            InvoiceItemPayment.objects.filter(iotec_configuration=config).update(is_active=False)
            return Response({'status': 'success', 'is_active': False})
        return self.activate_as_primary(request, pk=pk)