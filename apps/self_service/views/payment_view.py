from datetime import datetime
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404
import json

from ..permissions import CustomerOnlyPermission
from apps.billing.models import Invoice, Payment
from apps.billing.serializers import InvoiceSerializer, PaymentSerializer
from utils.mpesa_utils import MpesaService


class PaymentView(APIView):
    """
    Customer payment operations
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def get(self, request):
        """Get customer invoices and payments"""
        customer = request.user.customer_profile
        
        # Get invoices
        invoices = Invoice.objects.filter(customer=customer).order_by('-invoice_date')
        
        # Get payments
        payments = Payment.objects.filter(invoice__customer=customer).order_by('-payment_date')
        
        # Get payment methods
        payment_methods = self._get_payment_methods()
        
        return Response({
            'invoices': InvoiceSerializer(invoices, many=True).data,
            'payments': PaymentSerializer(payments, many=True).data,
            'payment_methods': payment_methods,
            'current_balance': float(customer.current_balance),
        })
    
    def post(self, request):
        """Make a payment"""
        customer = request.user.customer_profile
        
        amount = request.data.get('amount')
        payment_method = request.data.get('payment_method')
        invoice_id = request.data.get('invoice_id')
        phone_number = request.data.get('phone_number')  # For M-Pesa
        
        if not amount or not payment_method:
            return Response(
                {'error': 'Amount and payment method are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            amount = float(amount)
        except ValueError:
            return Response(
                {'error': 'Invalid amount'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate amount
        if amount <= 0:
            return Response(
                {'error': 'Amount must be greater than 0'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get invoice if specified
        invoice = None
        if invoice_id:
            invoice = get_object_or_404(Invoice, id=invoice_id, customer=customer)
        
        # Process payment based on method
        if payment_method == 'mpesa':
            return self._process_mpesa_payment(customer, amount, phone_number, invoice)
        elif payment_method == 'card':
            return self._process_card_payment(customer, amount, invoice)
        elif payment_method == 'bank':
            return self._process_bank_payment(customer, amount, invoice)
        else:
            return Response(
                {'error': 'Unsupported payment method'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    def _get_payment_methods(self):
        """Get available payment methods"""
        return [
            {
                'id': 'mpesa',
                'name': 'M-Pesa',
                'icon': 'phone',
                'description': 'Pay via M-Pesa mobile money',
                'enabled': True,
            },
            {
                'id': 'card',
                'name': 'Credit/Debit Card',
                'icon': 'credit-card',
                'description': 'Pay using Visa/Mastercard',
                'enabled': True,
            },
            {
                'id': 'bank',
                'name': 'Bank Transfer',
                'icon': 'bank',
                'description': 'Pay via bank transfer',
                'enabled': True,
            },
            {
                'id': 'voucher',
                'name': 'Payment Voucher',
                'icon': 'ticket',
                'description': 'Redeem payment voucher',
                'enabled': True,
            },
        ]
    
    def _process_mpesa_payment(self, customer, amount, phone_number, invoice):
        """Process M-Pesa payment"""
        if not phone_number:
            return Response(
                {'error': 'Phone number is required for M-Pesa payment'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate phone number
        from utils.validators import validate_phone_number
        if not validate_phone_number(phone_number):
            return Response(
                {'error': 'Invalid phone number'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Initialize M-Pesa service
        mpesa_service = MpesaService()
        
        # Generate STK push
        try:
            result = mpesa_service.generate_stk_push(
                phone_number=phone_number,
                amount=amount,
                account_reference=customer.customer_code,
                transaction_desc=f"Payment for {customer.name}"
            )
            
            if result.get('ResponseCode') == '0':
                # Payment initiated successfully
                return Response({
                    'message': 'Payment initiated. Please enter your M-Pesa PIN on your phone.',
                    'transaction_id': result.get('CheckoutRequestID'),
                    'status': 'pending',
                })
            else:
                return Response(
                    {'error': result.get('ResponseDescription', 'Payment failed')},
                    status=status.HTTP_400_BAD_REQUEST
                )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def _process_card_payment(self, customer, amount, invoice):
        """Process card payment (placeholder - integrate with payment gateway)"""
        # This would integrate with a payment gateway like Stripe
        return Response({
            'message': 'Card payment integration coming soon',
            'status': 'pending',
        }, status=status.HTTP_501_NOT_IMPLEMENTED)
    
    def _process_bank_payment(self, customer, amount, invoice):
        """Generate bank payment details"""
        # Generate unique payment reference
        import uuid
        payment_reference = f"ISP{customer.customer_code}{uuid.uuid4().hex[:8].upper()}"
        
        bank_details = {
            'bank_name': 'Cooperative Bank of Kenya',
            'account_name': 'Your ISP Name',
            'account_number': '0112345678900',
            'branch': 'Nairobi CBD',
            'swift_code': 'COOPKENXXX',
            'payment_reference': payment_reference,
            'amount': amount,
            'customer_name': customer.name,
        }
        
        return Response({
            'message': 'Please use the following bank details for payment',
            'bank_details': bank_details,
            'payment_reference': payment_reference,
            'status': 'pending',
        })