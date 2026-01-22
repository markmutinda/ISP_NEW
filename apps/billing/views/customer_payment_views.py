"""
Customer Payment Views

Endpoints for ISP customers to make payments (recharge, invoice payments).
These payments go to Netily's PayHero, with 95% settled to the ISP.
"""

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.services.payhero import PayHeroClient, PayHeroError, PaymentStatus
from apps.billing.models.payment_models import Payment
from apps.billing.models.billing_models import Invoice

logger = logging.getLogger(__name__)


class InitiateCustomerPaymentView(APIView):
    """
    Initiate customer payment via PayHero.
    
    POST /api/v1/billing/payments/initiate/
    {
        "amount": 2000,
        "phone_number": "254712345678",
        "invoice_id": 456,  // Optional
        "channel_id": 1     // Optional
    }
    """
    
    permission_classes = [IsAuthenticated]
    
    @transaction.atomic
    def post(self, request):
        user = request.user
        
        # Get customer profile
        from apps.customers.models import Customer
        
        try:
            customer = Customer.objects.get(user=user)
        except Customer.DoesNotExist:
            return Response(
                {'error': 'Customer profile not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        amount = request.data.get('amount')
        phone_number = request.data.get('phone_number')
        invoice_id = request.data.get('invoice_id')
        channel_id = request.data.get('channel_id')
        
        # Validate amount
        if not amount or Decimal(str(amount)) <= 0:
            return Response(
                {'error': 'Valid amount is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate phone number
        if not phone_number:
            # Use customer's phone number
            phone_number = customer.phone_number or user.phone_number
        
        if not phone_number:
            return Response(
                {'error': 'Phone number is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get invoice if specified
        invoice = None
        if invoice_id:
            try:
                invoice = Invoice.objects.get(id=invoice_id, customer=customer)
            except Invoice.DoesNotExist:
                return Response(
                    {'error': 'Invoice not found'},
                    status=status.HTTP_404_NOT_FOUND
                )
        
        # Create payment record
        payment = Payment.objects.create(
            customer=customer,
            invoice=invoice,
            amount=Decimal(str(amount)),
            phone_number=phone_number,
            payment_method='MPESA_STK',
            status='PENDING',
            notes=f"Customer initiated payment via dashboard",
        )
        
        # Generate reference
        reference = f"PAY-{customer.customer_id or customer.id}-{payment.id}"
        
        # Initiate PayHero STK Push
        try:
            client = PayHeroClient()
            
            description = f"Account Recharge - {customer.full_name}"
            if invoice:
                description = f"Invoice #{invoice.invoice_number}"
            
            response = client.stk_push(
                phone_number=phone_number,
                amount=int(amount),
                reference=reference,
                description=description,
                callback_url=settings.PAYHERO_BILLING_CALLBACK,
                channel_id=channel_id,
            )
            
            if response.success:
                payment.payhero_checkout_id = response.checkout_request_id
                payment.reference = reference
                payment.status = 'PROCESSING'
                payment.save()
                
                return Response({
                    'status': 'pending',
                    'payment_id': payment.id,
                    'payhero_response': {
                        'status': 'pending',
                        'checkout_request_id': response.checkout_request_id,
                        'message': 'STK Push sent to your phone',
                    }
                })
            else:
                payment.status = 'FAILED'
                payment.failure_reason = response.message
                payment.save()
                
                return Response({
                    'status': 'error',
                    'message': response.message or 'Failed to initiate payment',
                }, status=status.HTTP_400_BAD_REQUEST)
        
        except PayHeroError as e:
            logger.error(f"Customer payment PayHero error: {e.message}")
            payment.status = 'FAILED'
            payment.failure_reason = str(e)
            payment.save()
            
            return Response({
                'status': 'error',
                'message': 'Payment service unavailable. Please try again.',
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CustomerPaymentStatusView(APIView):
    """
    Poll customer payment status.
    
    GET /api/v1/billing/payments/{id}/status/
    """
    
    permission_classes = [IsAuthenticated]
    
    def get(self, request, payment_id):
        user = request.user
        
        # Get customer's payment
        try:
            from apps.customers.models import Customer
            customer = Customer.objects.get(user=user)
            payment = Payment.objects.get(id=payment_id, customer=customer)
        except (Customer.DoesNotExist, Payment.DoesNotExist):
            return Response(
                {'error': 'Payment not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Return current status if already finalized
        if payment.status in ['COMPLETED', 'FAILED', 'CANCELLED']:
            return Response({
                'payment_id': payment.id,
                'status': payment.status.lower(),
                'message': self._get_status_message(payment),
                'mpesa_receipt': payment.mpesa_receipt_number,
                'amount': float(payment.amount),
                'completed_at': payment.paid_at,
            })
        
        # Check with PayHero if pending
        if payment.payhero_checkout_id:
            try:
                client = PayHeroClient()
                status_response = client.get_payment_status(payment.payhero_checkout_id)
                
                if status_response.status == PaymentStatus.SUCCESS:
                    payment.status = 'COMPLETED'
                    payment.mpesa_receipt_number = status_response.mpesa_receipt
                    payment.paid_at = timezone.now()
                    payment.save()
                    
                    # Apply to customer
                    customer = payment.customer
                    if payment.invoice:
                        payment.invoice.apply_payment(payment)
                    else:
                        customer.balance = (customer.balance or 0) + payment.amount
                        customer.save(update_fields=['balance'])
                    
                    return Response({
                        'payment_id': payment.id,
                        'status': 'completed',
                        'message': 'Payment successful!',
                        'mpesa_receipt': payment.mpesa_receipt_number,
                        'amount': float(payment.amount),
                        'completed_at': payment.paid_at,
                        'new_balance': float(customer.balance or 0),
                    })
                
                elif status_response.status == PaymentStatus.FAILED:
                    payment.status = 'FAILED'
                    payment.failure_reason = status_response.failure_reason
                    payment.save()
                    
                    return Response({
                        'payment_id': payment.id,
                        'status': 'failed',
                        'message': status_response.failure_reason or 'Payment failed',
                        'mpesa_receipt': None,
                        'amount': float(payment.amount),
                        'completed_at': None,
                    })
            
            except PayHeroError as e:
                logger.error(f"Error checking payment status: {e.message}")
        
        # Still pending
        return Response({
            'payment_id': payment.id,
            'status': 'pending',
            'message': 'Waiting for payment confirmation...',
            'mpesa_receipt': None,
            'amount': float(payment.amount),
            'completed_at': None,
        })
    
    def _get_status_message(self, payment):
        messages = {
            'COMPLETED': 'Payment successful!',
            'FAILED': payment.failure_reason or 'Payment failed',
            'CANCELLED': 'Payment was cancelled',
            'PENDING': 'Waiting for payment...',
            'PROCESSING': 'Processing payment...',
        }
        return messages.get(payment.status, 'Unknown status')


class CustomerPaymentMethodsView(APIView):
    """
    Get available payment methods for customer.
    
    GET /api/v1/billing/payment-methods/
    """
    
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        from apps.billing.models.payment_models import InvoiceItemPayment
        
        # Get active payment methods
        methods = InvoiceItemPayment.objects.filter(
            is_active=True,
        ).order_by('name')
        
        methods_data = [
            {
                'id': method.id,
                'code': method.code,
                'name': method.name,
                'method_type': method.method_type,
                'description': method.description,
                'is_payhero_enabled': method.is_payhero_enabled,
                'channel_id': method.channel_id,
                'minimum_amount': float(method.minimum_amount),
                'maximum_amount': float(method.maximum_amount),
                'transaction_fee': float(method.transaction_fee),
                'fee_type': method.fee_type,
            }
            for method in methods
        ]
        
        return Response({
            'payment_methods': methods_data,
            'default_method': 'mpesa_stk',  # STK Push is default
        })
