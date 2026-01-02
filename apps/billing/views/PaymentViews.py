from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.db.models import Q, Sum, Count
from decimal import Decimal
import json

# Use your existing permissions
from apps.core.permissions import IsCompanyAdmin, IsCompanyStaff, IsCompanyMember
from apps.customers.models import Customer
from ..models.billing_models import Invoice
from ..models.payment_models import Payment, PaymentMethod, Receipt
from ..serializers import (
    PaymentSerializer, PaymentMethodSerializer, ReceiptSerializer,
    PaymentCreateSerializer, PaymentDetailSerializer, MpesaSTKPushSerializer
)
from ..integrations.mpesa_integration import MpesaSTKPush, MpesaCallback, MpesaValidation
from ..integrations.africastalking import SMSService


class PaymentMethodViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing payment methods
    """
    queryset = PaymentMethod.objects.all()
    serializer_class = PaymentMethodSerializer
    permission_classes = [IsAuthenticated, IsCompanyAdmin]  # Changed to IsCompanyAdmin
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['method_type', 'is_active', 'status']
    search_fields = ['name', 'code', 'description']
    
    def get_queryset(self):
        """Filter payment methods by company"""
        user = self.request.user
        if user.is_superuser:
            return PaymentMethod.objects.all()
        return PaymentMethod.objects.filter(company=user.company)
    
    def perform_create(self, serializer):
        """Set created_by and company on creation"""
        serializer.save(
            created_by=self.request.user,
            company=self.request.user.company
        )
    
    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """Toggle payment method active status"""
        payment_method = self.get_object()
        payment_method.is_active = not payment_method.is_active
        payment_method.save()
        return Response({'status': 'success', 'is_active': payment_method.is_active})
    
    @action(detail=True, methods=['post'])
    def test_connection(self, request, pk=None):
        """Test payment method connection"""
        payment_method = self.get_object()
        
        if payment_method.method_type == 'MPESA':
            # Test M-Pesa connection
            mpesa = MpesaSTKPush(payment_method.company)
            result = mpesa._get_access_token()
            
            if result:
                return Response({
                    'status': 'success',
                    'message': 'M-Pesa connection successful'
                })
            else:
                return Response({
                    'status': 'error',
                    'message': 'M-Pesa connection failed'
                })
        
        return Response({
            'status': 'info',
            'message': f'No test available for {payment_method.method_type}'
        })


class PaymentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing payments
    """
    queryset = Payment.objects.all()
    permission_classes = [IsAuthenticated, IsCompanyStaff]  # Changed to IsCompanyStaff
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'payment_method', 'customer', 'is_reconciled']
    search_fields = ['payment_number', 'customer__customer_code', 'transaction_id', 'mpesa_receipt']
    ordering_fields = ['payment_date', 'amount', 'created_at']
    
    def get_serializer_class(self):
        """Return appropriate serializer based on action"""
        if self.action == 'create':
            return PaymentCreateSerializer
        elif self.action == 'retrieve':
            return PaymentDetailSerializer
        return PaymentSerializer
    
    def get_queryset(self):
        """Filter payments by company and user role"""
        user = self.request.user
        queryset = Payment.objects.all()
        
        if user.is_superuser:
            return queryset
        
        queryset = queryset.filter(company=user.company)
        
        # Customers can only see their own payments
        if user.role == 'customer' and hasattr(user, 'customer_profile'):
            return queryset.filter(customer=user.customer_profile)
        
        return queryset
    
    def perform_create(self, serializer):
        """Set created_by and company on payment creation"""
        serializer.save(
            created_by=self.request.user,
            company=self.request.user.company
        )
    
    @action(detail=True, methods=['post'])
    def mark_completed(self, request, pk=None):
        """Mark payment as completed"""
        payment = self.get_object()
        if payment.mark_as_completed(request.user):
            
            # Send SMS confirmation if configured
            try:
                sms_service = SMSService(payment.company)
                sms_service.send_payment_confirmation(payment.customer, payment)
            except Exception as e:
                # Log but don't fail the payment
                pass
            
            return Response({'status': 'success', 'message': 'Payment marked as completed'})
        return Response(
            {'status': 'error', 'message': 'Cannot mark payment as completed'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    @action(detail=True, methods=['post'])
    def mark_failed(self, request, pk=None):
        """Mark payment as failed"""
        payment = self.get_object()
        reason = request.data.get('reason', '')
        if payment.mark_as_failed(reason):
            return Response({'status': 'success', 'message': 'Payment marked as failed'})
        return Response(
            {'status': 'error', 'message': 'Cannot mark payment as failed'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    @action(detail=True, methods=['post'])
    def reconcile(self, request, pk=None):
        """Reconcile payment"""
        payment = self.get_object()
        if not payment.is_reconciled:
            payment.is_reconciled = True
            payment.reconciled_at = timezone.now()
            payment.reconciled_by = request.user
            payment.save()
            return Response({'status': 'success', 'message': 'Payment reconciled'})
        return Response(
            {'status': 'error', 'message': 'Payment already reconciled'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    @action(detail=True, methods=['post'])
    def refund(self, request, pk=None):
        """Refund payment"""
        payment = self.get_object()
        refund_amount = Decimal(request.data.get('refund_amount', 0))
        refund_reason = request.data.get('refund_reason', '')
        
        if refund_amount <= 0:
            refund_amount = None  # Full refund
        
        refund_payment = payment.refund(refund_amount, refund_reason)
        
        if refund_payment:
            return Response({
                'status': 'success',
                'message': 'Refund processed',
                'refund_payment_id': refund_payment.id
            })
        
        return Response(
            {'status': 'error', 'message': 'Cannot process refund'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    @action(detail=False, methods=['post'])
    def mpesa_stk_push(self, request):
        """Initiate M-Pesa STK Push payment"""
        serializer = MpesaSTKPushSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        customer_id = data.get('customer_id')
        invoice_id = data.get('invoice_id')
        amount = data.get('amount')
        phone_number = data.get('phone_number')
        
        # Get customer
        from customers.models import Customer
        try:
            customer = Customer.objects.get(id=customer_id)
        except Customer.DoesNotExist:
            return Response(
                {'status': 'error', 'message': 'Customer not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Get invoice if provided
        invoice = None
        if invoice_id:
            try:
                invoice = Invoice.objects.get(id=invoice_id, customer=customer)
            except Invoice.DoesNotExist:
                return Response(
                    {'status': 'error', 'message': 'Invoice not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
        
        # Validate amount
        is_valid_amount, amount_error = MpesaValidation.validate_amount(amount)
        if not is_valid_amount:
            return Response(
                {'status': 'error', 'message': amount_error},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate phone number
        is_valid_phone, formatted_phone, phone_error = MpesaValidation.validate_phone_number(phone_number)
        if not is_valid_phone:
            return Response(
                {'status': 'error', 'message': phone_error},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get M-Pesa payment method
        try:
            payment_method = PaymentMethod.objects.get(
                method_type='MPESA',
                company=request.user.company,
                is_active=True
            )
        except PaymentMethod.DoesNotExist:
            return Response(
                {'status': 'error', 'message': 'M-Pesa payment method not configured'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Create pending payment record
        payment = Payment.objects.create(
            company=request.user.company,
            customer=customer,
            invoice=invoice,
            amount=amount,
            payment_method=payment_method,
            status='PENDING',
            payer_phone=formatted_phone,
            created_by=request.user
        )
        
        # Initiate STK Push
        mpesa = MpesaSTKPush(request.user.company)
        
        account_reference = invoice.invoice_number if invoice else f"CUST-{customer.customer_code}"
        transaction_desc = f"Payment for {account_reference}"
        
        result = mpesa.initiate_stk_push(
            phone_number=formatted_phone,
            amount=amount,
            account_reference=account_reference,
            transaction_desc=transaction_desc
        )
        
        if result['success']:
            # Update payment with M-Pesa details
            payment.transaction_id = result['data']['checkout_request_id']
            payment.payment_reference = result['data']['merchant_request_id']
            payment.save()
            
            return Response({
                'status': 'success',
                'message': result['message'],
                'payment_id': payment.id,
                'checkout_request_id': result['data']['checkout_request_id'],
                'customer_message': result['data']['customer_message']
            })
        else:
            # Update payment as failed
            payment.status = 'FAILED'
            payment.failure_reason = result.get('message', 'STK Push failed')
            payment.save()
            
            return Response({
                'status': 'error',
                'message': result.get('message', 'Failed to initiate STK Push'),
                'payment_id': payment.id
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['post'])
    def mpesa_callback(self, request):
        """Handle M-Pesa callback"""
        callback_data = request.data
        
        # Process callback
        callback_handler = MpesaCallback()
        result = callback_handler.handle_stk_callback(callback_data)
        
        if result['status'] == 'SUCCESS':
            # Find payment by checkout request ID
            checkout_request_id = result['checkout_request_id']
            
            try:
                payment = Payment.objects.get(
                    transaction_id=checkout_request_id,
                    status='PENDING'
                )
            except Payment.DoesNotExist:
                return Response(
                    {'status': 'error', 'message': 'Payment not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            # Update payment with transaction details
            transaction_data = result['transaction_data']
            
            payment.mpesa_receipt = transaction_data['mpesa_receipt']
            payment.mpesa_phone = transaction_data['phone_number']
            payment.payment_date = timezone.now()
            
            # Mark as completed
            payment.mark_as_completed(payment.created_by)
            
            # Send SMS notification
            try:
                sms_service = SMSService(payment.company)
                sms_service.send_payment_confirmation(payment.customer, payment)
            except Exception as e:
                # Log but don't fail
                pass
            
            return Response({
                'status': 'success',
                'message': 'Payment processed successfully',
                'payment_id': payment.id,
                'receipt_number': payment.mpesa_receipt
            })
        
        else:
            # Failed transaction
            checkout_request_id = result['checkout_request_id']
            
            try:
                payment = Payment.objects.get(transaction_id=checkout_request_id)
                payment.status = 'FAILED'
                payment.failure_reason = result.get('error_message', 'Transaction failed')
                payment.save()
            except Payment.DoesNotExist:
                pass
            
            return Response({
                'status': 'error',
                'message': result.get('error_message', 'Transaction failed')
            }, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=False, methods=['post'])
    def bank_transfer(self, request):
        """Process bank transfer payment"""
        # Get payment details
        customer_id = request.data.get('customer_id')
        invoice_id = request.data.get('invoice_id')
        amount = Decimal(request.data.get('amount', 0))
        bank_name = request.data.get('bank_name')
        account_number = request.data.get('account_number')
        transaction_reference = request.data.get('transaction_reference')
        
        # Validate inputs
        if amount <= 0:
            return Response(
                {'status': 'error', 'message': 'Invalid amount'},
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
        
        # Get invoice if provided
        invoice = None
        if invoice_id:
            try:
                invoice = Invoice.objects.get(id=invoice_id, customer=customer)
            except Invoice.DoesNotExist:
                return Response(
                    {'status': 'error', 'message': 'Invoice not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
        
        # Get bank transfer payment method
        try:
            payment_method = PaymentMethod.objects.get(
                method_type='BANK_TRANSFER',
                company=request.user.company,
                is_active=True
            )
        except PaymentMethod.DoesNotExist:
            return Response(
                {'status': 'error', 'message': 'Bank transfer payment method not configured'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Create payment record
        payment = Payment.objects.create(
            company=request.user.company,
            customer=customer,
            invoice=invoice,
            amount=amount,
            payment_method=payment_method,
            status='PENDING',
            payment_reference=transaction_reference,
            bank_name=bank_name,
            account_number=account_number,
            payer_name=customer.full_name,
            payer_phone=customer.user.phone_number,
            created_by=request.user
        )
        
        return Response({
            'status': 'success',
            'message': 'Bank transfer payment recorded',
            'payment_id': payment.id,
            'payment_number': payment.payment_number
        })
    
    @action(detail=False, methods=['get'])
    def dashboard_stats(self, request):
        """Get payment dashboard statistics"""
        queryset = self.get_queryset()
        
        today = timezone.now().date()
        yesterday = today - timezone.timedelta(days=1)
        
        # Today's statistics
        today_payments = queryset.filter(payment_date__date=today)
        today_count = today_payments.count()
        today_amount = today_payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        
        # Yesterday's statistics
        yesterday_payments = queryset.filter(payment_date__date=yesterday)
        yesterday_count = yesterday_payments.count()
        yesterday_amount = yesterday_payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        
        # This month statistics
        month_start = today.replace(day=1)
        month_payments = queryset.filter(payment_date__date__gte=month_start)
        month_count = month_payments.count()
        month_amount = month_payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        
        # Status distribution
        status_counts = {
            'PENDING': queryset.filter(status='PENDING').count(),
            'COMPLETED': queryset.filter(status='COMPLETED').count(),
            'FAILED': queryset.filter(status='FAILED').count(),
            'REFUNDED': queryset.filter(status='REFUNDED').count(),
        }
        
        # Payment method distribution
        method_distribution = queryset.values('payment_method__name').annotate(
            count=Count('id'),
            total=Sum('amount')
        ).order_by('-total')
        
        # Top payers (last 30 days)
        thirty_days_ago = today - timezone.timedelta(days=30)
        top_payers = queryset.filter(
            payment_date__date__gte=thirty_days_ago,
            status='COMPLETED'
        ).values('customer__customer_code', 'customer__user__first_name', 'customer__user__last_name').annotate(
            total_paid=Sum('amount'),
            payment_count=Count('id')
        ).order_by('-total_paid')[:10]
        
        stats = {
            'today': {
                'count': today_count,
                'amount': today_amount
            },
            'yesterday': {
                'count': yesterday_count,
                'amount': yesterday_amount
            },
            'this_month': {
                'count': month_count,
                'amount': month_amount
            },
            'status_distribution': status_counts,
            'method_distribution': list(method_distribution),
            'top_payers': list(top_payers)
        }
        
        return Response(stats)


class ReceiptViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing receipts
    """
    queryset = Receipt.objects.all()
    serializer_class = ReceiptSerializer
    permission_classes = [IsAuthenticated, IsCompanyStaff]  # Changed to IsCompanyStaff
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['status', 'customer', 'company']
    search_fields = ['receipt_number', 'customer__customer_code']
    
    def get_queryset(self):
        """Filter receipts by company and user role"""
        user = self.request.user
        queryset = Receipt.objects.all()
        
        if user.is_superuser:
            return queryset
        
        queryset = queryset.filter(company=user.company)
        
        # Customers can only see their own receipts
        if user.role == 'customer' and hasattr(user, 'customer_profile'):
            return queryset.filter(customer=user.customer_profile)
        
        return queryset
    
    def perform_create(self, serializer):
        """Set created_by and company on receipt creation"""
        serializer.save(
            created_by=self.request.user,
            company=self.request.user.company
        )
    
    @action(detail=True, methods=['post'])
    def issue(self, request, pk=None):
        """Issue receipt"""
        receipt = self.get_object()
        if receipt.issue_receipt(request.user):
            return Response({'status': 'success', 'message': 'Receipt issued'})
        return Response(
            {'status': 'error', 'message': 'Cannot issue receipt'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    @action(detail=True, methods=['get'])
    def download_pdf(self, request, pk=None):
        """Download receipt as PDF"""
        receipt = self.get_object()
        
        # Generate PDF (you'll need to implement this)
        # For now, return receipt data
        serializer = self.get_serializer(receipt)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def share(self, request, pk=None):
        """Share receipt via email/SMS"""
        receipt = self.get_object()
        share_method = request.query_params.get('method', 'email')
        
        if share_method == 'email':
            # Send receipt via email
            # Implement email sending logic
            return Response({
                'status': 'success',
                'message': 'Receipt sent via email'
            })
        elif share_method == 'sms':
            # Send receipt via SMS
            try:
                sms_service = SMSService(receipt.company)
                message = f"Receipt {receipt.receipt_number} for KES {receipt.amount} issued. Thank you!"
                result = sms_service.send_single_sms(
                    receipt.customer.user.phone_number,
                    message
                )
                
                if result['success']:
                    return Response({
                        'status': 'success',
                        'message': 'Receipt sent via SMS'
                    })
                else:
                    return Response({
                        'status': 'error',
                        'message': 'Failed to send SMS'
                    })
            except Exception as e:
                return Response({
                    'status': 'error',
                    'message': str(e)
                })
        
        return Response({
            'status': 'error',
            'message': 'Invalid share method'
        })