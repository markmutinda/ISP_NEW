"""
Payment Status View

Endpoint for checking payment status.
"""

import logging

from django.shortcuts import get_object_or_404

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import Payment, Invoice
from apps.core.models import AuditLog
from ..permissions import CustomerOnlyPermission

logger = logging.getLogger(__name__)


class PaymentStatusView(APIView):
    """
    Check the status of a payment.
    
    AUTHENTICATED ENDPOINT - Requires customer JWT token.
    
    GET /api/v1/self-service/payments/{payment_id}/status/
    
    Response includes:
    - payment_status: PENDING, PROCESSING, COMPLETED, FAILED
    - mpesa_reference: M-Pesa transaction reference (if completed)
    - invoice_status: Whether the associated invoice is now paid
    """
    
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def get(self, request, payment_id):
        user = request.user
        
        # Get the customer's profile
        try:
            customer = user.customer_profile
        except Exception:
            return Response({
                'error': 'Customer profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Get the payment (only customer's own payments)
        payment = get_object_or_404(
            Payment.objects.select_related('invoice'),
            id=payment_id,
            customer=customer
        )
        
        # Build response
        response_data = {
            'payment_id': str(payment.id),
            'status': payment.status,
            'amount': float(payment.amount),
            'payment_method': payment.payment_method,
            'created_at': payment.created_at.isoformat(),
            'updated_at': payment.updated_at.isoformat(),
        }
        
        # Include M-Pesa details if available
        if payment.payment_method == 'mpesa':
            response_data.update({
                'mpesa_reference': payment.transaction_reference,
                'phone_number': payment.phone_number,
            })
        
        # Include invoice status
        if payment.invoice:
            response_data['invoice'] = {
                'id': str(payment.invoice.id),
                'invoice_number': payment.invoice.invoice_number,
                'status': payment.invoice.status,
                'total_amount': float(payment.invoice.total_amount),
                'amount_paid': float(payment.invoice.amount_paid),
                'balance_due': float(payment.invoice.total_amount - payment.invoice.amount_paid),
            }
        
        # Include any error message for failed payments
        if payment.status == 'FAILED':
            response_data['error_message'] = getattr(payment, 'error_message', 'Payment failed')
        
        return Response(response_data)


class PaymentRefreshStatusView(APIView):
    """
    Force a status refresh for a pending payment.
    
    AUTHENTICATED ENDPOINT - Requires customer JWT token.
    
    POST /api/v1/self-service/payments/{payment_id}/refresh/
    
    This queries PayHero API to get the latest status for pending payments.
    """
    
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def post(self, request, payment_id):
        user = request.user
        
        try:
            customer = user.customer_profile
        except Exception:
            return Response({
                'error': 'Customer profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Get the payment
        payment = get_object_or_404(
            Payment,
            id=payment_id,
            customer=customer
        )
        
        # Only refresh pending payments
        if payment.status not in ['PENDING', 'PROCESSING']:
            return Response({
                'message': 'Payment already completed',
                'status': payment.status,
            })
        
        # Try to query PayHero for current status
        try:
            from utils.payhero_utils import query_payment_status
            
            payhero_status = query_payment_status(payment.checkout_request_id)
            
            if payhero_status:
                # Update payment based on PayHero response
                if payhero_status.get('success'):
                    payment.status = 'COMPLETED'
                    payment.transaction_reference = payhero_status.get('mpesa_reference')
                    payment.save()
                    
                    # Update invoice if associated
                    if payment.invoice:
                        payment.invoice.record_payment(payment.amount, payment.transaction_reference)
                elif payhero_status.get('failed'):
                    payment.status = 'FAILED'
                    payment.error_message = payhero_status.get('error_message', 'Payment failed')
                    payment.save()
                
                return Response({
                    'status': payment.status,
                    'message': 'Status refreshed',
                    'mpesa_reference': payment.transaction_reference,
                })
        
        except Exception as e:
            logger.error(f"Error refreshing payment status: {e}")
        
        return Response({
            'status': payment.status,
            'message': 'Unable to refresh status. Please try again.',
        })


class CustomerPaymentsListView(APIView):
    """
    List all payments for the authenticated customer.
    
    AUTHENTICATED ENDPOINT - Requires customer JWT token.
    
    GET /api/v1/self-service/payments/
    """
    
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def get(self, request):
        user = request.user
        
        try:
            customer = user.customer_profile
        except Exception:
            return Response({
                'error': 'Customer profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Get payments
        payments = Payment.objects.filter(customer=customer).order_by('-created_at')[:20]
        
        payments_data = [
            {
                'id': str(payment.id),
                'amount': float(payment.amount),
                'status': payment.status,
                'payment_method': payment.payment_method,
                'transaction_reference': payment.transaction_reference,
                'created_at': payment.created_at.isoformat(),
                'invoice_number': payment.invoice.invoice_number if payment.invoice else None,
            }
            for payment in payments
        ]
        
        return Response({
            'payments': payments_data,
            'count': len(payments_data),
        })
