from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.db.models import Q, Sum
from decimal import Decimal
import json

# Use your existing permissions
from apps.core.permissions import IsCompanyAdmin, IsCompanyStaff, IsCompanyMember
from apps.customers.models import Customer
from ..models.billing_models import Invoice, InvoiceItem, Plan, BillingCycle
from ..serializers import (
    InvoiceSerializer, InvoiceItemSerializer, 
    InvoiceCreateSerializer, InvoiceDetailSerializer,
    PlanSerializer, BillingCycleSerializer
)
from ..calculators.invoice_calculator import InvoiceCalculator


class PlanViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing internet plans
    """
    queryset = Plan.objects.all()
    serializer_class = PlanSerializer
    permission_classes = [IsAuthenticated, IsCompanyStaff]  # Changed to IsCompanyStaff
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['plan_type', 'is_active', 'billing_cycle', 'company']
    search_fields = ['name', 'code', 'description']
    ordering_fields = ['name', 'base_price', 'download_speed', 'created_at']
    
    def get_queryset(self):
        """Filter plans by company"""
        user = self.request.user
        if user.is_superuser:
            return Plan.objects.all()
        return Plan.objects.filter(company=user.company)
    
    def perform_create(self, serializer):
        """Set created_by and company on plan creation"""
        serializer.save(
            created_by=self.request.user,
            company=self.request.user.company
        )
    
    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """Toggle plan active status"""
        plan = self.get_object()
        plan.is_active = not plan.is_active
        plan.save()
        return Response({'status': 'success', 'is_active': plan.is_active})
    
    @action(detail=False, methods=['get'])
    def public(self, request):
        """Get public plans"""
        plans = self.get_queryset().filter(is_public=True, is_active=True)
        serializer = self.get_serializer(plans, many=True)
        return Response(serializer.data)


class BillingCycleViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing billing cycles
    """
    queryset = BillingCycle.objects.all()
    serializer_class = BillingCycleSerializer
    permission_classes = [IsAuthenticated, IsCompanyStaff]  # Changed to IsCompanyStaff
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'is_locked', 'company']
    search_fields = ['cycle_code', 'name', 'notes']
    ordering_fields = ['start_date', 'end_date', 'created_at']
    
    def get_queryset(self):
        """Filter billing cycles by company"""
        user = self.request.user
        if user.is_superuser:
            return BillingCycle.objects.all()
        return BillingCycle.objects.filter(company=user.company)
    
    def perform_create(self, serializer):
        """Set created_by and company on creation"""
        serializer.save(
            created_by=self.request.user,
            company=self.request.user.company
        )
    
    @action(detail=True, methods=['post'])
    def close_cycle(self, request, pk=None):
        """Close billing cycle"""
        billing_cycle = self.get_object()
        if billing_cycle.close_cycle(request.user):
            return Response({'status': 'success', 'message': 'Billing cycle closed'})
        return Response(
            {'status': 'error', 'message': 'Cannot close billing cycle'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    @action(detail=True, methods=['post'])
    def calculate_totals(self, request, pk=None):
        """Calculate cycle totals"""
        billing_cycle = self.get_object()
        billing_cycle.calculate_totals()
        return Response({'status': 'success', 'message': 'Totals calculated'})
    
    @action(detail=False, methods=['get'])
    def current(self, request):
        """Get current billing cycle"""
        today = timezone.now().date()
        billing_cycle = self.get_queryset().filter(
            start_date__lte=today,
            end_date__gte=today,
            status='OPEN'
        ).first()
        
        if billing_cycle:
            serializer = self.get_serializer(billing_cycle)
            return Response(serializer.data)
        return Response(
            {'status': 'error', 'message': 'No current billing cycle'},
            status=status.HTTP_404_NOT_FOUND
        )
    
    @action(detail=True, methods=['get'])
    def summary(self, request, pk=None):
        """Get billing cycle summary"""
        billing_cycle = self.get_object()
        
        # Calculate summary
        from ..models.payment_models import Payment
        from customers.models import Customer
        
        total_customers = Customer.objects.filter(company=billing_cycle.company).count()
        active_customers = Customer.objects.filter(
            company=billing_cycle.company,
            status='ACTIVE'
        ).count()
        
        total_invoices = billing_cycle.invoices.count()
        paid_invoices = billing_cycle.invoices.filter(status='PAID').count()
        overdue_invoices = billing_cycle.invoices.filter(status='OVERDUE').count()
        
        total_payments = Payment.objects.filter(
            invoice__billing_cycle=billing_cycle,
            status='COMPLETED'
        ).count()
        
        summary = {
            'cycle': billing_cycle.cycle_code,
            'total_customers': total_customers,
            'active_customers': active_customers,
            'total_invoices': total_invoices,
            'paid_invoices': paid_invoices,
            'overdue_invoices': overdue_invoices,
            'collection_rate': (paid_invoices / total_invoices * 100) if total_invoices > 0 else 0,
            'total_payments': total_payments,
            'total_amount': billing_cycle.total_amount,
            'total_paid': billing_cycle.total_paid,
            'total_outstanding': billing_cycle.total_outstanding,
        }
        
        return Response(summary)


class InvoiceViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing invoices
    """
    queryset = Invoice.objects.all()
    permission_classes = [IsAuthenticated, IsCompanyStaff]  # Changed to IsCompanyStaff
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'is_overdue', 'customer', 'billing_cycle']
    search_fields = ['invoice_number', 'customer__customer_code', 'customer__user__first_name', 'customer__user__last_name']
    ordering_fields = ['billing_date', 'due_date', 'total_amount', 'created_at']
    
    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action == 'create':
            return InvoiceCreateSerializer
        elif self.action == 'retrieve':
            return InvoiceDetailSerializer
        return InvoiceSerializer
    
    def get_queryset(self):
        """Filter invoices by company and user role"""
        user = self.request.user
        queryset = Invoice.objects.all()
        
        if user.is_superuser:
            return queryset
        
        queryset = queryset.filter(company=user.company)
        
        # Customers can only see their own invoices
        if user.role == 'customer' and hasattr(user, 'customer_profile'):
            return queryset.filter(customer=user.customer_profile)
        
        return queryset
    
    def perform_create(self, serializer):
        """Set created_by and company on invoice creation"""
        serializer.save(
            created_by=self.request.user,
            company=self.request.user.company
        )
    
    @action(detail=True, methods=['post'])
    def issue(self, request, pk=None):
        """Issue an invoice"""
        invoice = self.get_object()
        if invoice.issue_invoice(request.user):
            return Response({'status': 'success', 'message': 'Invoice issued'})
        return Response(
            {'status': 'error', 'message': 'Cannot issue invoice'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    @action(detail=True, methods=['post'])
    def mark_as_sent(self, request, pk=None):
        """Mark invoice as sent"""
        invoice = self.get_object()
        if invoice.mark_as_sent():
            return Response({'status': 'success', 'message': 'Invoice marked as sent'})
        return Response(
            {'status': 'error', 'message': 'Cannot mark invoice as sent'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    @action(detail=True, methods=['post'])
    def add_payment(self, request, pk=None):
        """Add payment to invoice"""
        invoice = self.get_object()
        amount = Decimal(request.data.get('amount', 0))
        payment_method_id = request.data.get('payment_method_id')
        
        if amount <= 0:
            return Response(
                {'status': 'error', 'message': 'Invalid amount'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from ..models.payment_models import PaymentMethod
        try:
            payment_method = PaymentMethod.objects.get(id=payment_method_id)
        except PaymentMethod.DoesNotExist:
            return Response(
                {'status': 'error', 'message': 'Invalid payment method'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        payment = invoice.add_payment(amount, payment_method)
        
        return Response({
            'status': 'success',
            'message': 'Payment added',
            'payment_id': payment.id,
            'new_balance': invoice.balance
        })
    
    @action(detail=True, methods=['post'])
    def apply_discount(self, request, pk=None):
        """Apply discount to invoice"""
        invoice = self.get_object()
        discount_amount = Decimal(request.data.get('discount_amount', 0))
        discount_reason = request.data.get('discount_reason', '')
        
        try:
            invoice = InvoiceCalculator.apply_discount(invoice, discount_amount, discount_reason)
            return Response({
                'status': 'success',
                'message': 'Discount applied',
                'new_total': invoice.total_amount
            })
        except ValueError as e:
            return Response(
                {'status': 'error', 'message': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    @action(detail=True, methods=['get'])
    def items(self, request, pk=None):
        """Get invoice items"""
        invoice = self.get_object()
        items = invoice.items.all()
        serializer = InvoiceItemSerializer(items, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def payments(self, request, pk=None):
        """Get invoice payments"""
        invoice = self.get_object()
        payments = invoice.payments.all()
        from ..serializers import PaymentSerializer
        serializer = PaymentSerializer(payments, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['post'])
    def bulk_generate(self, request):
        """Bulk generate invoices for active services"""
        billing_cycle_id = request.data.get('billing_cycle_id')
        
        try:
            billing_cycle = BillingCycle.objects.get(id=billing_cycle_id)
        except BillingCycle.DoesNotExist:
            return Response(
                {'status': 'error', 'message': 'Invalid billing cycle'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        invoices = InvoiceCalculator.generate_bulk_invoices(
            request.user.company,
            billing_cycle
        )
        
        return Response({
            'status': 'success',
            'message': f'Generated {len(invoices)} invoices',
            'invoice_count': len(invoices)
        })
    
    @action(detail=False, methods=['get'])
    def overdue(self, request):
        """Get overdue invoices"""
        overdue_invoices = self.get_queryset().filter(
            status='OVERDUE',
            balance__gt=0
        )
        serializer = self.get_serializer(overdue_invoices, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def dashboard_stats(self, request):
        """Get invoice dashboard statistics"""
        queryset = self.get_queryset()
        
        total_invoices = queryset.count()
        total_amount = queryset.aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0')
        total_paid = queryset.aggregate(Sum('amount_paid'))['amount_paid__sum'] or Decimal('0')
        total_outstanding = queryset.aggregate(Sum('balance'))['balance__sum'] or Decimal('0')
        
        status_counts = {
            'DRAFT': queryset.filter(status='DRAFT').count(),
            'ISSUED': queryset.filter(status='ISSUED').count(),
            'SENT': queryset.filter(status='SENT').count(),
            'PARTIAL': queryset.filter(status='PARTIAL').count(),
            'PAID': queryset.filter(status='PAID').count(),
            'OVERDUE': queryset.filter(status='OVERDUE').count(),
        }
        
        overdue_amount = queryset.filter(status='OVERDUE').aggregate(
            Sum('balance')
        )['balance__sum'] or Decimal('0')
        
        collection_rate = (total_paid / total_amount * 100) if total_amount > 0 else 0
        
        stats = {
            'total_invoices': total_invoices,
            'total_amount': total_amount,
            'total_paid': total_paid,
            'total_outstanding': total_outstanding,
            'overdue_amount': overdue_amount,
            'collection_rate': collection_rate,
            'status_counts': status_counts,
        }
        
        return Response(stats)
    
    @action(detail=False, methods=['get'])
    def customer_outstanding(self, request):
        """Get customer outstanding balances"""
        from apps.customers.models import Customer
        from django.db.models import Sum
        
        customers = Customer.objects.filter(company=request.user.company)
        
        outstanding_data = []
        for customer in customers:
            outstanding = InvoiceCalculator.calculate_outstanding_balance(customer)
            if outstanding > 0:
                outstanding_data.append({
                    'customer_id': customer.id,
                    'customer_code': customer.customer_code,
                    'customer_name': customer.full_name,
                    'phone': customer.user.phone_number,
                    'outstanding_balance': outstanding,
                    'invoice_count': customer.invoices.filter(
                        status__in=['ISSUED', 'SENT', 'PARTIAL', 'OVERDUE'],
                        balance__gt=0
                    ).count()
                })
        
        # Sort by outstanding balance (highest first)
        outstanding_data.sort(key=lambda x: x['outstanding_balance'], reverse=True)
        
        return Response(outstanding_data)


class InvoiceItemViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing invoice items
    """
    queryset = InvoiceItem.objects.all()
    serializer_class = InvoiceItemSerializer
    permission_classes = [IsAuthenticated, IsCompanyStaff]  # Changed to IsCompanyStaff
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['invoice']
    
    def get_queryset(self):
        """Filter invoice items by company"""
        user = self.request.user
        if user.is_superuser:
            return InvoiceItem.objects.all()
        
        # Filter by invoice's company
        return InvoiceItem.objects.filter(invoice__company=user.company)
    
    def perform_create(self, serializer):
        """Set invoice on item creation"""
        invoice_id = self.request.data.get('invoice')
        try:
            invoice = Invoice.objects.get(id=invoice_id)
            serializer.save(invoice=invoice)
        except Invoice.DoesNotExist:
            return Response(
                {'status': 'error', 'message': 'Invalid invoice'},
                status=status.HTTP_400_BAD_REQUEST
            )