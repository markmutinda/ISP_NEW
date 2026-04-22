from django.db.models import Q, Sum, Count
from django.utils import timezone
from django_filters import rest_framework as filters
from rest_framework import viewsets, generics, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.filters import SearchFilter, OrderingFilter
from datetime import timedelta

from apps.core.permissions import IsAdmin, IsAdminOrStaff

from .models import (
    Supplier, EquipmentType, EquipmentItem, Assignment,
    PurchaseOrder, PurchaseOrderItem, MaintenanceRecord, StockAlert
)
from .serializers import (
    SupplierSerializer, EquipmentTypeSerializer, EquipmentItemSerializer,
    AssignmentSerializer, PurchaseOrderSerializer, PurchaseOrderItemSerializer,
    MaintenanceRecordSerializer, StockAlertSerializer,
)
from .filters import EquipmentItemFilter, PurchaseOrderFilter


class SupplierViewSet(viewsets.ModelViewSet):
    queryset = Supplier.objects.filter(is_active=True)
    serializer_class = SupplierSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'contact_person', 'email', 'phone']
    ordering_fields = ['name', 'created_at']

    def get_queryset(self):
        return super().get_queryset().annotate(
            total_purchases=Sum('equipment__purchase_price'),
            equipment_count=Count('equipment')
        )

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save()

    @action(detail=True, methods=['get'])
    def equipment(self, request, pk=None):
        supplier = self.get_object()
        serializer = EquipmentItemSerializer(supplier.equipment.all(), many=True)
        return Response(serializer.data)


class EquipmentTypeViewSet(viewsets.ModelViewSet):
    queryset = EquipmentType.objects.filter(is_active=True)
    serializer_class = EquipmentTypeSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'created_at']

    def get_queryset(self):
        return super().get_queryset().annotate(
            item_count=Count('items'),
            available_count=Count('items', filter=Q(items__status='in_stock'))
        )


class EquipmentItemViewSet(viewsets.ModelViewSet):
    queryset = EquipmentItem.objects.select_related(
        'equipment_type', 'supplier', 'assigned_to', 'assigned_to_customer'
    ).all()
    serializer_class = EquipmentItemSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = EquipmentItemFilter
    search_fields = ['name', 'model', 'serial_number', 'asset_tag', 'mac_address', 'notes']
    ordering_fields = ['name', 'purchase_date', 'purchase_price', 'status', 'created_at']
    ordering = ['-created_at']

    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy',
                           'assign', 'return_item', 'maintenance', 'dispose']:
            return [IsAuthenticated(), IsAdminOrStaff()]
        return super().get_permissions()

    @action(detail=False, methods=['get'])
    def available(self, request):
        """Get equipment available for assignment"""
        qs = self.get_queryset().filter(
            status='in_stock', condition__in=['new', 'good', 'fair']
        )
        page = self.paginate_queryset(qs)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(qs, many=True).data)

    @action(detail=True, methods=['post'])
    def assign(self, request, pk=None):
        """Assign equipment to employee. Accepts employee_id (string like EMP001 or PK int)."""
        equipment = self.get_object()

        if not equipment.is_available:
            return Response(
                {'error': f'Equipment is not available. Current status: {equipment.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        employee_id = request.data.get('employee_id')
        purpose = request.data.get('purpose', '')
        expected_return = request.data.get('expected_return_date')

        if not employee_id:
            return Response(
                {'error': 'employee_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            from apps.staff.models import Employee
            # Try by employee_id string field first, then by PK
            try:
                employee = Employee.objects.get(employee_id=employee_id)
            except Employee.DoesNotExist:
                employee = Employee.objects.get(pk=employee_id)

            assignment = Assignment.objects.create(
                equipment=equipment,
                assigned_to=employee,
                assigned_by=request.user,
                assigned_date=timezone.now().date(),
                expected_return_date=expected_return or None,
                condition_on_assignment=equipment.condition,
                purpose=purpose,
            )
            equipment.status = 'assigned'
            equipment.assigned_to = employee
            equipment.save()

            return Response(AssignmentSerializer(assignment).data)

        except Exception:  # noqa: BLE001
            return Response(
                {'error': f'Employee with id "{employee_id}" not found'},
                status=status.HTTP_404_NOT_FOUND
            )

    @action(detail=True, methods=['post'])
    def return_item(self, request, pk=None):
        """Return assigned equipment back to stock."""
        equipment = self.get_object()

        if equipment.status != 'assigned':
            return Response(
                {'error': 'Equipment is not currently assigned'},
                status=status.HTTP_400_BAD_REQUEST
            )

        condition = request.data.get('condition', equipment.condition)
        notes = request.data.get('notes', '')

        assignment = Assignment.objects.filter(
            equipment=equipment,
            actual_return_date__isnull=True
        ).first()

        if assignment:
            assignment.notes = (assignment.notes or '') + ('\n' + notes if notes else '')
            assignment.mark_returned(condition)
        else:
            # No assignment record — just update status directly
            equipment.status = 'in_stock'
            equipment.assigned_to = None
            equipment.condition = condition
            equipment.save()

        return Response(EquipmentItemSerializer(equipment).data)

    @action(detail=True, methods=['post'])
    def maintenance(self, request, pk=None):
        """Send equipment to maintenance."""
        equipment = self.get_object()

        notes = request.data.get('notes', '')
        description = request.data.get('description', 'Sent to maintenance')
        maintenance_type = request.data.get('maintenance_type', 'Corrective')

        previous_status = equipment.status
        equipment.status = 'maintenance'
        equipment.save()

        MaintenanceRecord.objects.create(
            equipment=equipment,
            scheduled_date=timezone.now().date(),
            status='in_progress',
            maintenance_type=maintenance_type,
            description=description or notes or 'Sent to maintenance via admin panel',
        )

        return Response(EquipmentItemSerializer(equipment).data)

    @action(detail=True, methods=['post'])
    def dispose(self, request, pk=None):
        """Mark equipment as disposed."""
        equipment = self.get_object()

        reason = request.data.get('reason', 'Disposed via admin panel')
        equipment.status = 'disposed'
        if reason:
            equipment.notes = (equipment.notes or '') + f'\nDisposed: {reason}'
        equipment.save()

        return Response(EquipmentItemSerializer(equipment).data)

    @action(detail=False, methods=['get'])
    def report(self, request):
        report_type = request.query_params.get('type', 'summary')

        if report_type == 'summary':
            summary = EquipmentItem.objects.values('equipment_type__name').annotate(
                total_count=Count('id'),
                in_stock=Count('id', filter=Q(status='in_stock')),
                assigned=Count('id', filter=Q(status='assigned')),
                in_use=Count('id', filter=Q(status='in_use')),
                under_maintenance=Count('id', filter=Q(status='maintenance')),
                total_value=Sum('purchase_price')
            )
            data = [{
                'equipment_type': item['equipment_type__name'],
                'total_count': item['total_count'],
                'in_stock': item['in_stock'],
                'assigned': item['assigned'],
                'in_use': item['in_use'],
                'under_maintenance': item['under_maintenance'],
                'total_value': item['total_value'] or 0,
            } for item in summary]
            return Response(data)

        elif report_type == 'warranty':
            thirty_days = timezone.now().date() + timedelta(days=30)
            expiring = EquipmentItem.objects.filter(
                warranty_expiry__lte=thirty_days,
                warranty_expiry__gte=timezone.now().date()
            ).order_by('warranty_expiry')
            return Response(self.get_serializer(expiring, many=True).data)

        return Response({'error': 'Invalid report type'}, status=status.HTTP_400_BAD_REQUEST)


class AssignmentViewSet(viewsets.ModelViewSet):
    queryset = Assignment.objects.select_related(
        'equipment', 'assigned_to', 'assigned_by'
    ).all()
    serializer_class = AssignmentSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [filters.DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['assigned_to', 'equipment']
    ordering_fields = ['assigned_date', 'created_at']

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get('active', '').lower() == 'true':
            qs = qs.filter(actual_return_date__isnull=True)
        return qs

    @action(detail=True, methods=['post'])
    def mark_returned(self, request, pk=None):
        assignment = self.get_object()
        if assignment.actual_return_date:
            return Response(
                {'error': 'Assignment already returned'},
                status=status.HTTP_400_BAD_REQUEST
            )
        condition = request.data.get('condition')
        if not condition:
            return Response({'error': 'Condition is required'}, status=status.HTTP_400_BAD_REQUEST)
        assignment.mark_returned(condition)
        return Response(self.get_serializer(assignment).data)


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    queryset = PurchaseOrder.objects.all()
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [filters.DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = PurchaseOrderFilter
    search_fields = ['po_number', 'supplier__name', 'notes']
    ordering_fields = ['order_date', 'total_amount', 'status']

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        po = self.get_object()
        if po.status != 'pending':
            return Response({'error': 'Only pending orders can be approved'}, status=400)
        po.status = 'approved'
        po.approved_by = request.user
        po.approved_date = timezone.now()
        po.save()
        return Response(self.get_serializer(po).data)


class PurchaseOrderItemViewSet(viewsets.ModelViewSet):
    queryset = PurchaseOrderItem.objects.all()
    serializer_class = PurchaseOrderItemSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get_queryset(self):
        qs = super().get_queryset()
        po_id = self.request.query_params.get('purchase_order')
        if po_id:
            qs = qs.filter(purchase_order_id=po_id)
        return qs


class MaintenanceRecordViewSet(viewsets.ModelViewSet):
    queryset = MaintenanceRecord.objects.all()
    serializer_class = MaintenanceRecordSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [filters.DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['equipment', 'status']
    ordering_fields = ['scheduled_date', 'completed_date']

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        record = self.get_object()
        if record.status == 'completed':
            return Response({'error': 'Already completed'}, status=400)
        record.status = 'completed'
        record.completed_date = timezone.now().date()
        record.action_taken = request.data.get('action_taken', '')
        record.cost = request.data.get('cost', 0)
        if request.data.get('next_maintenance_date'):
            record.next_maintenance_date = request.data['next_maintenance_date']
        record.save()
        new_condition = request.data.get('equipment_condition')
        if new_condition:
            record.equipment.condition = new_condition
            record.equipment.status = 'in_stock'
            record.equipment.save()
        return Response(self.get_serializer(record).data)


class StockAlertViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = StockAlert.objects.filter(is_active=True)
    serializer_class = StockAlertSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    @action(detail=False, methods=['get'])
    def check_stock(self, request):
        alerts_created = 0
        for eq_type in EquipmentType.objects.filter(is_active=True):
            in_stock = EquipmentItem.objects.filter(
                equipment_type=eq_type, status='in_stock'
            ).count()
            if in_stock <= eq_type.min_stock_level:
                existing = StockAlert.objects.filter(equipment_type=eq_type, is_active=True).exists()
                if not existing:
                    StockAlert.objects.create(
                        equipment_type=eq_type,
                        threshold=eq_type.min_stock_level,
                        current_stock=in_stock,
                    )
                    alerts_created += 1
        return Response({'alerts_created': alerts_created})


class StockReportView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        report_data = EquipmentItem.objects.values('equipment_type__name').annotate(
            total_quantity=Count('id'),
            total_value=Sum('purchase_price'),
            available_quantity=Count('id', filter=Q(status='in_stock')),
            assigned_quantity=Count('id', filter=Q(status='assigned')),
        ).order_by('equipment_type__name')

        fmt = request.query_params.get('format', 'json')
        if fmt == 'csv':
            import csv
            from django.http import HttpResponse
            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename="inventory_report.csv"'
            writer = csv.writer(response)
            writer.writerow(['Equipment Type', 'Total', 'Available', 'Assigned', 'Value'])
            for item in report_data:
                writer.writerow([
                    item['equipment_type__name'], item['total_quantity'],
                    item['available_quantity'], item['assigned_quantity'],
                    item['total_value'] or 0,
                ])
            return response

        return Response(list(report_data))