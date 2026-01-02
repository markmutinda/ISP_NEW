from django.db.models import Q, Sum, Count, F
from django.utils import timezone
from django_filters import rest_framework as filters
from rest_framework import viewsets, generics, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.filters import SearchFilter, OrderingFilter
from datetime import timedelta
import pandas as pd

# Correct permissions import
from apps.core.permissions import IsAdmin, IsAdminOrStaff

from .models import (
    Supplier, EquipmentType, EquipmentItem, Assignment,
    PurchaseOrder, PurchaseOrderItem, MaintenanceRecord, StockAlert
)
from .serializers import (
    SupplierSerializer, EquipmentTypeSerializer, EquipmentItemSerializer,
    AssignmentSerializer, PurchaseOrderSerializer, PurchaseOrderItemSerializer,
    MaintenanceRecordSerializer, StockAlertSerializer,
    EquipmentReportSerializer, StockMovementSerializer
)
from .filters import EquipmentItemFilter, PurchaseOrderFilter


class SupplierViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for suppliers
    """
    queryset = Supplier.objects.filter(is_active=True)
    serializer_class = SupplierSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]  # Staff + Admin access
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'contact_person', 'email', 'phone']
    ordering_fields = ['name', 'created_at']
   
    def get_queryset(self):
        queryset = super().get_queryset()
       
        # Calculate total purchases for each supplier
        queryset = queryset.annotate(
            total_purchases=Sum('equipment__purchase_price'),
            equipment_count=Count('equipment')
        )
       
        return queryset
   
    def perform_destroy(self, instance):
        # Soft delete instead of actual delete
        instance.is_active = False
        instance.save()
   
    @action(detail=True, methods=['get'])
    def equipment(self, request, pk=None):
        """Get equipment from this supplier"""
        supplier = self.get_object()
        equipment = supplier.equipment.all()
        serializer = EquipmentItemSerializer(equipment, many=True)
        return Response(serializer.data)


class EquipmentTypeViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for equipment types
    """
    queryset = EquipmentType.objects.all()
    serializer_class = EquipmentTypeSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'created_at']
   
    def get_queryset(self):
        queryset = super().get_queryset()
       
        # Annotate with counts
        queryset = queryset.annotate(
            item_count=Count('items'),
            available_count=Count(
                'items',
                filter=Q(items__status='in_stock')
            )
        )
       
        return queryset
   
    @action(detail=False, methods=['get'])
    def categories(self, request):
        """Get equipment type categories (parent types)"""
        categories = EquipmentType.objects.filter(parent__isnull=True)
        serializer = self.get_serializer(categories, many=True)
        return Response(serializer.data)


class EquipmentItemViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for equipment items
    """
    queryset = EquipmentItem.objects.all()
    serializer_class = EquipmentItemSerializer
    permission_classes = [IsAuthenticated]  # Everyone can see, only staff can modify
    filter_backends = [filters.DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = EquipmentItemFilter
    search_fields = [
        'name', 'model', 'serial_number', 'asset_tag',
        'mac_address', 'notes'
    ]
    ordering_fields = [
        'name', 'purchase_date', 'purchase_price',
        'status', 'created_at'
    ]
   
    def get_queryset(self):
        queryset = super().get_queryset()
       
        # Staff can see all, customers can only see their assigned equipment
        if not self.request.user.is_staff:
            queryset = queryset.filter(
                assigned_to__user=self.request.user
            )
       
        return queryset
   
    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy', 'assign', 'return_item']:
            return [IsAuthenticated(), IsAdminOrStaff()]
        return super().get_permissions()
   
    @action(detail=False, methods=['get'])
    def available(self, request):
        """Get available equipment for assignment"""
        queryset = self.get_queryset().filter(
            status='in_stock',
            condition__in=['new', 'good', 'fair']
        )
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
   
    @action(detail=True, methods=['post'])
    def assign(self, request, pk=None):
        """Assign equipment to employee"""
        equipment = self.get_object()
        employee_id = request.data.get('employee_id')
        purpose = request.data.get('purpose')
        expected_return = request.data.get('expected_return_date')
       
        if not employee_id or not purpose:
            return Response(
                {'error': 'employee_id and purpose are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
       
        # Check if equipment is available
        if not equipment.is_available():
            return Response(
                {'error': 'Equipment is not available for assignment'},
                status=status.HTTP_400_BAD_REQUEST
            )
       
        try:
            from apps.staff.models import Employee
            employee = Employee.objects.get(id=employee_id)
           
            # Create assignment
            assignment = Assignment.objects.create(
                equipment=equipment,
                assigned_to=employee,
                assigned_by=request.user,
                assigned_date=timezone.now().date(),
                expected_return_date=expected_return,
                condition_on_assignment=equipment.condition,
                purpose=purpose
            )
           
            # Update equipment status
            equipment.status = 'assigned'
            equipment.assigned_to = employee
            equipment.save()
           
            serializer = AssignmentSerializer(assignment)
            return Response(serializer.data)
       
        except Employee.DoesNotExist:
            return Response(
                {'error': 'Employee not found'},
                status=status.HTTP_404_NOT_FOUND
            )
   
    @action(detail=True, methods=['post'])
    def return_item(self, request, pk=None):
        """Return assigned equipment"""
        equipment = self.get_object()
       
        if equipment.status != 'assigned':
            return Response(
                {'error': 'Equipment is not assigned'},
                status=status.HTTP_400_BAD_REQUEST
            )
       
        condition = request.data.get('condition', equipment.condition)
       
        # Find active assignment
        assignment = Assignment.objects.filter(
            equipment=equipment,
            actual_return_date__isnull=True
        ).first()
       
        if assignment:
            assignment.mark_returned(condition)
            return Response({'message': 'Equipment returned successfully'})
       
        return Response(
            {'error': 'No active assignment found'},
            status=status.HTTP_400_BAD_REQUEST
        )
   
    @action(detail=False, methods=['get'])
    def report(self, request):
        """Generate equipment report"""
        report_type = request.query_params.get('type', 'summary')
       
        if report_type == 'summary':
            # Summary report by equipment type
            summary = EquipmentItem.objects.values(
                'equipment_type__name'
            ).annotate(
                total_count=Count('id'),
                in_stock=Count('id', filter=Q(status='in_stock')),
                assigned=Count('id', filter=Q(status='assigned')),
                in_use=Count('id', filter=Q(status='in_use')),
                under_maintenance=Count('id', filter=Q(status='maintenance')),
                total_value=Sum('purchase_price')
            )
           
            data = []
            for item in summary:
                data.append({
                    'equipment_type': item['equipment_type__name'],
                    'total_count': item['total_count'],
                    'in_stock': item['in_stock'],
                    'assigned': item['assigned'],
                    'in_use': item['in_use'],
                    'under_maintenance': item['under_maintenance'],
                    'total_value': item['total_value'] or 0
                })
           
            serializer = EquipmentReportSerializer(data, many=True)
            return Response(serializer.data)
       
        elif report_type == 'warranty':
            # Warranty expiry report
            thirty_days_from_now = timezone.now().date() + timedelta(days=30)
            expiring_soon = EquipmentItem.objects.filter(
                warranty_expiry__lte=thirty_days_from_now,
                warranty_expiry__gte=timezone.now().date()
            ).order_by('warranty_expiry')
           
            serializer = self.get_serializer(expiring_soon, many=True)
            return Response(serializer.data)
       
        return Response(
            {'error': 'Invalid report type'},
            status=status.HTTP_400_BAD_REQUEST
        )


class AssignmentViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for equipment assignments
    """
    queryset = Assignment.objects.all()
    serializer_class = AssignmentSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [filters.DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['assigned_to', 'equipment', 'status']
    ordering_fields = ['assigned_date', 'created_at']
   
    def get_queryset(self):
        queryset = super().get_queryset()
       
        # Filter by active assignments if requested
        active_only = self.request.query_params.get('active', 'false').lower() == 'true'
        if active_only:
            queryset = queryset.filter(actual_return_date__isnull=True)
       
        return queryset
   
    @action(detail=True, methods=['post'])
    def mark_returned(self, request, pk=None):
        """Mark assignment as returned"""
        assignment = self.get_object()
       
        if assignment.actual_return_date:
            return Response(
                {'error': 'Assignment already returned'},
                status=status.HTTP_400_BAD_REQUEST
            )
       
        condition = request.data.get('condition')
        if not condition:
            return Response(
                {'error': 'Condition is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
       
        assignment.mark_returned(condition)
        serializer = self.get_serializer(assignment)
        return Response(serializer.data)


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for purchase orders
    """
    queryset = PurchaseOrder.objects.all()
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [filters.DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = PurchaseOrderFilter
    search_fields = ['po_number', 'supplier__name', 'notes']
    ordering_fields = ['order_date', 'total_amount', 'status']
   
    def get_queryset(self):
        queryset = super().get_queryset()
       
        # Filter by status if provided
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
       
        return queryset
   
    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approve a purchase order"""
        purchase_order = self.get_object()
       
        if purchase_order.status != 'pending':
            return Response(
                {'error': 'Only pending orders can be approved'},
                status=status.HTTP_400_BAD_REQUEST
            )
       
        purchase_order.status = 'approved'
        purchase_order.approved_by = request.user
        purchase_order.approved_date = timezone.now()
        purchase_order.save()
       
        serializer = self.get_serializer(purchase_order)
        return Response(serializer.data)
   
    @action(detail=True, methods=['post'])
    def receive(self, request, pk=None):
        """Receive items from purchase order"""
        purchase_order = self.get_object()
       
        if purchase_order.status not in ['approved', 'ordered']:
            return Response(
                {'error': 'Order must be approved or ordered before receiving'},
                status=status.HTTP_400_BAD_REQUEST
            )
       
        items_data = request.data.get('items', [])
       
        # Update received quantities
        for item_data in items_data:
            try:
                po_item = PurchaseOrderItem.objects.get(
                    id=item_data['id'],
                    purchase_order=purchase_order
                )
                received_qty = item_data.get('received_quantity', 0)
                po_item.received_quantity = received_qty
                po_item.save()
               
                # Create equipment items if fully received (future expansion)
                if received_qty > 0 and po_item.equipment_type.has_serial_numbers:
                    pass
               
            except PurchaseOrderItem.DoesNotExist:
                continue
       
        # Check if all items are received
        all_received = all(
            item.received_quantity >= item.quantity
            for item in purchase_order.items.all()
        )
       
        if all_received:
            purchase_order.status = 'received'
            purchase_order.actual_delivery = timezone.now().date()
       
        purchase_order.save()
        serializer = self.get_serializer(purchase_order)
        return Response(serializer.data)


class PurchaseOrderItemViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for purchase order items
    """
    queryset = PurchaseOrderItem.objects.all()
    serializer_class = PurchaseOrderItemSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
   
    def get_queryset(self):
        queryset = super().get_queryset()
       
        # Filter by purchase order if provided
        po_id = self.request.query_params.get('purchase_order')
        if po_id:
            queryset = queryset.filter(purchase_order_id=po_id)
       
        return queryset


class MaintenanceRecordViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for maintenance records
    """
    queryset = MaintenanceRecord.objects.all()
    serializer_class = MaintenanceRecordSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [filters.DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['equipment', 'status', 'performed_by']
    ordering_fields = ['scheduled_date', 'completed_date']
   
    @action(detail=False, methods=['get'])
    def upcoming(self, request):
        """Get upcoming maintenance schedules"""
        from_date = timezone.now().date()
        to_date = from_date + timedelta(days=30)
       
        upcoming = self.get_queryset().filter(
            scheduled_date__gte=from_date,
            scheduled_date__lte=to_date,
            status='scheduled'
        ).order_by('scheduled_date')
       
        serializer = self.get_serializer(upcoming, many=True)
        return Response(serializer.data)
   
    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Complete a maintenance record"""
        record = self.get_object()
       
        if record.status == 'completed':
            return Response(
                {'error': 'Maintenance already completed'},
                status=status.HTTP_400_BAD_REQUEST
            )
       
        action_taken = request.data.get('action_taken')
        cost = request.data.get('cost', 0)
        next_maintenance = request.data.get('next_maintenance_date')
       
        record.status = 'completed'
        record.completed_date = timezone.now().date()
        record.action_taken = action_taken
        record.cost = cost
       
        if next_maintenance:
            record.next_maintenance_date = next_maintenance
       
        record.save()
       
        # Update equipment condition if provided
        new_condition = request.data.get('equipment_condition')
        if new_condition:
            record.equipment.condition = new_condition
            record.equipment.status = 'in_stock'
            record.equipment.save()
       
        serializer = self.get_serializer(record)
        return Response(serializer.data)


class StockAlertViewSet(viewsets.ReadOnlyModelViewSet):
    """
    View stock alerts
    """
    queryset = StockAlert.objects.filter(is_active=True)
    serializer_class = StockAlertSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
   
    @action(detail=False, methods=['get'])
    def check_stock(self, request):
        """Check stock levels and create alerts if needed"""
        from .models import EquipmentType, EquipmentItem
       
        # Get all equipment types that require monitoring
        equipment_types = EquipmentType.objects.filter(
            has_serial_numbers=True
        )
       
        alerts_created = 0
        for eq_type in equipment_types:
            in_stock_count = EquipmentItem.objects.filter(
                equipment_type=eq_type,
                status='in_stock'
            ).count()
           
            # Check if alert threshold is reached
            threshold = 5  # This should be configurable per equipment type
           
            if in_stock_count <= threshold:
                # Check if active alert already exists
                existing_alert = StockAlert.objects.filter(
                    equipment_type=eq_type,
                    is_active=True
                ).exists()
               
                if not existing_alert:
                    StockAlert.objects.create(
                        equipment_type=eq_type,
                        threshold=threshold,
                        current_stock=in_stock_count
                    )
                    alerts_created += 1
       
        return Response({
            'message': f'Created {alerts_created} new stock alerts',
            'alerts_created': alerts_created
        })


class StockReportView(generics.GenericAPIView):
    """
    Generate stock reports
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
   
    def get(self, request):
        report_type = request.query_params.get('type', 'inventory')
        format = request.query_params.get('format', 'json')
       
        if report_type == 'inventory':
            # Inventory valuation report
            from django.db.models import Sum, Count
           
            report_data = EquipmentItem.objects.values(
                'equipment_type__name'
            ).annotate(
                total_quantity=Count('id'),
                total_value=Sum('purchase_price'),
                available_quantity=Count('id', filter=Q(status='in_stock')),
                assigned_quantity=Count('id', filter=Q(status='assigned'))
            ).order_by('equipment_type__name')
           
            if format == 'csv':
                # Generate CSV response
                import csv
                from django.http import HttpResponse
               
                response = HttpResponse(content_type='text/csv')
                response['Content-Disposition'] = 'attachment; filename="inventory_report.csv"'
               
                writer = csv.writer(response)
                writer.writerow([
                    'Equipment Type', 'Total Quantity', 'Available Quantity',
                    'Assigned Quantity', 'Total Value'
                ])
               
                for item in report_data:
                    writer.writerow([
                        item['equipment_type__name'],
                        item['total_quantity'],
                        item['available_quantity'],
                        item['assigned_quantity'],
                        item['total_value'] or 0
                    ])
               
                return response
           
            return Response(report_data)
       
        elif report_type == 'movement':
            # Stock movement report (placeholder for future expansion)
            pass
       
        return Response(
            {'error': 'Invalid report type'},
            status=status.HTTP_400_BAD_REQUEST
        )