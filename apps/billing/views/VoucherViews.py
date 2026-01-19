from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.db.models import Q, Sum, Count
from decimal import Decimal

# Use your existing permissions
from apps.core.permissions import IsCompanyAdmin, IsCompanyStaff, IsCompanyMember
from apps.customers.models import Customer
from ..models.voucher_models import VoucherBatch, Voucher
from ..serializers import (
    VoucherBatchSerializer, VoucherSerializer, VoucherUsageSerializer,
    VoucherBatchCreateSerializer, VoucherCreateSerializer,
    VoucherRedeemSerializer
)
from ..integrations.africastalking import SMSService


class VoucherBatchViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing voucher batches
    """
    queryset = VoucherBatch.objects.all()
    permission_classes = [IsAuthenticated, IsCompanyAdmin]  # Changed to IsCompanyAdmin
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['voucher_type', 'status', 'is_active']
    search_fields = ['batch_number', 'name', 'description']
    ordering_fields = ['created_at', 'valid_from', 'valid_to']
    
    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action == 'create':
            return VoucherBatchCreateSerializer
        return VoucherBatchSerializer
    
    def get_queryset(self):
        """Filter voucher batches by company"""
        user = self.request.user
        if user.is_superuser:
            return VoucherBatch.objects.all()
        return VoucherBatch.objects.filter(company=user.company)
    
    def perform_create(self, serializer):
        """Set created_by and company on batch creation"""
        serializer.save(
            created_by=self.request.user,
            company=self.request.user.company
        )
    
    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """Activate voucher batch"""
        batch = self.get_object()
        if batch.activate_batch(request.user):
            return Response({'status': 'success', 'message': 'Voucher batch activated'})
        return Response(
            {'status': 'error', 'message': 'Cannot activate voucher batch'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    @action(detail=True, methods=['post'])
    def generate_vouchers(self, request, pk=None):
        """Generate vouchers for batch"""
        batch = self.get_object()
        count = request.data.get('count')
        
        vouchers = batch.generate_vouchers(count)
        
        return Response({
            'status': 'success',
            'message': f'Generated {len(vouchers)} vouchers',
            'voucher_count': len(vouchers),
            'vouchers': VoucherSerializer(vouchers, many=True).data
        })
    
    @action(detail=True, methods=['get'])
    def vouchers(self, request, pk=None):
        """Get vouchers in batch"""
        batch = self.get_object()
        vouchers = batch.vouchers.all()
        serializer = VoucherSerializer(vouchers, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def statistics(self, request, pk=None):
        """Get batch statistics"""
        batch = self.get_object()
        
        total_value = batch.face_value * batch.quantity
        issued_value = batch.face_value * batch.issued_count
        used_value = batch.face_value * batch.used_count
        available_value = total_value - issued_value
        
        usage_rate = (batch.used_count / batch.issued_count * 100) if batch.issued_count > 0 else 0
        
        stats = {
            'batch': batch.batch_number,
            'total_quantity': batch.quantity,
            'issued_count': batch.issued_count,
            'used_count': batch.used_count,
            'available_count': batch.available_count,
            'total_value': total_value,
            'issued_value': issued_value,
            'used_value': used_value,
            'available_value': available_value,
            'usage_rate': usage_rate,
            'status': batch.status,
            'valid_from': batch.valid_from,
            'valid_to': batch.valid_to,
        }
        
        return Response(stats)


class VoucherViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing vouchers
    """
    queryset = Voucher.objects.all()
    serializer_class = VoucherSerializer
    permission_classes = [IsAuthenticated, IsCompanyStaff]  # Changed to IsCompanyStaff
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'batch', 'is_reusable', 'sold_to']
    search_fields = ['code', 'pin', 'batch__name']
    ordering_fields = ['created_at', 'valid_to', 'face_value']
    
    def get_queryset(self):
        """Filter vouchers by company"""
        user = self.request.user
        if user.is_superuser:
            return Voucher.objects.all()
        return Voucher.objects.filter(batch__company=user.company)
    
    def perform_create(self, serializer):
        """Set created_by on voucher creation"""
        serializer.save(created_by=self.request.user)
    
    @action(detail=True, methods=['post'])
    def sell(self, request, pk=None):
        """Sell voucher to customer"""
        voucher = self.get_object()
        customer_id = request.data.get('customer_id')
        
        if not customer_id:
            return Response(
                {'status': 'error', 'message': 'Customer ID required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get customer
        from customers.models import Customer
        try:
            customer = Customer.objects.get(id=customer_id)
        except Customer.DoesNotExist:
            return Response(
                {'status': 'error', 'message': 'Customer not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Sell voucher
        success, message = voucher.sell_voucher(customer, request.user)
        
        if success:
            # Send voucher PIN via SMS
            try:
                sms_service = SMSService(voucher.batch.company)
                sms_service.send_voucher_pin(customer, voucher)
            except Exception as e:
                # Log but don't fail
                pass
            
            return Response({'status': 'success', 'message': message})
        
        return Response(
            {'status': 'error', 'message': message},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    @action(detail=True, methods=['post'])
    def redeem(self, request, pk=None):
        """Redeem voucher for payment"""
        voucher = self.get_object()
        serializer = VoucherRedeemSerializer(data=request.data)
        
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        customer_id = data.get('customer_id')
        amount = data.get('amount')
        description = data.get('description', '')
        
        # Get customer
        from customers.models import Customer
        try:
            customer = Customer.objects.get(id=customer_id)
        except Customer.DoesNotExist:
            return Response(
                {'status': 'error', 'message': 'Customer not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Use voucher
        payment, message = voucher.use_voucher(customer, amount, description)
        
        if payment:
            return Response({
                'status': 'success',
                'message': message,
                'payment_id': payment.id,
                'remaining_balance': voucher.remaining_value
            })
        
        return Response(
            {'status': 'error', 'message': message},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    @action(detail=True, methods=['post'])
    def check_validity(self, request, pk=None):
        """Check voucher validity"""
        voucher = self.get_object()
        is_valid = voucher.is_valid()
        
        return Response({
            'status': 'success',
            'is_valid': is_valid,
            'remaining_value': voucher.remaining_value,
            'use_count': voucher.use_count,
            'max_uses': voucher.max_uses,
            'valid_until': voucher.valid_to
        })
    
    @action(detail=False, methods=['post'])
    def validate_code(self, request):
        """Validate voucher by code"""
        code = request.data.get('code')
        pin = request.data.get('pin')
        
        if not code:
            return Response(
                {'status': 'error', 'message': 'Voucher code required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            voucher = Voucher.objects.get(code=code)
            
            # Check PIN if provided
            if pin and voucher.pin != pin:
                return Response({
                    'status': 'error',
                    'message': 'Invalid PIN'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            serializer = self.get_serializer(voucher)
            return Response({
                'status': 'success',
                'voucher': serializer.data,
                'is_valid': voucher.is_valid()
            })
            
        except Voucher.DoesNotExist:
            return Response(
                {'status': 'error', 'message': 'Voucher not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    
    @action(detail=True, methods=['get'])
    def usage_history(self, request, pk=None):
        """Get voucher usage history"""
        voucher = self.get_object()
        usages = voucher.usages.all()
        serializer = VoucherUsageSerializer(usages, many=True)
        return Response(serializer.data)


"""
class VoucherUsageViewSet(viewsets.ReadOnlyModelViewSet):
    '''
    ViewSet for viewing voucher usages (read-only)
    '''
    queryset = VoucherUsage.objects.all()
    serializer_class = VoucherUsageSerializer
    permission_classes = [IsAuthenticated, IsCompanyStaff]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['voucher', 'customer', 'payment', 'invoice']
    ordering_fields = ['created_at', 'amount']
    
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser:
            return VoucherUsage.objects.all()
        return VoucherUsage.objects.filter(voucher__batch__company=user.company)
    
    @action(detail=False, methods=['get'])
    def customer_history(self, request):
        customer_id = request.query_params.get('customer_id')
        
        if not customer_id:
            return Response(
                {'status': 'error', 'message': 'Customer ID required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        from customers.models import Customer
        try:
            customer = Customer.objects.get(id=customer_id)
        except Customer.DoesNotExist:
            return Response(
                {'status': 'error', 'message': 'Customer not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        usages = self.get_queryset().filter(customer=customer)
        serializer = self.get_serializer(usages, many=True)
        
        total_used = usages.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        usage_count = usages.count()
        
        return Response({
            'customer': {
                'id': customer.id,
                'code': customer.customer_code,
                'name': customer.full_name
            },
            'total_vouchers_used': usage_count,
            'total_amount_used': total_used,
            'usages': serializer.data
        })
"""
