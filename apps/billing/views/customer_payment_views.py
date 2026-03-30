"""
Customer Payment Views

Endpoints for ISP customers to make payments (recharge, invoice payments).
These payments go through the Tuma Payment Gateway.
"""

import logging
import time
from decimal import Decimal

from django.conf import settings
from django.db import transaction, connection
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models.payment_models import Payment, InvoiceItemPayment, TenantTumaConfig
from apps.billing.models.billing_models import Invoice
from apps.billing.services.tuma_service import TumaClient, TumaError

logger = logging.getLogger(__name__)


class InitiateCustomerPaymentView(APIView):
    """
    Initiate customer payment via Tuma Gateway.
    
    POST /api/v1/billing/payments/initiate/
    {
        "amount": 2000,
        "phone_number": "254712345678",
        "invoice_id": 456   // Optional
    }
    """
    
    permission_classes = [IsAuthenticated]
    
    def _get_or_create_tuma_payment_method(self):
        """Get or create generic STK payment method for Tuma"""
        method, created = InvoiceItemPayment.objects.get_or_create(
            code='TUMA_STK',
            defaults={
                'name': 'M-Pesa STK Push (Tuma)',
                'method_type': 'MPESA_STK',
                'is_active': True,
            }
        )
        return method
    
    @transaction.atomic
    def post(self, request):
        user = request.user
        schema = connection.schema_name
        
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
        
        # Validate amount
        if not amount or Decimal(str(amount)) <= 0:
            return Response(
                {'error': 'Valid amount is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate phone number
        if not phone_number:
            phone_number = customer.phone_number or getattr(user, 'phone_number', None)
        
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
        
        # Verify Tenant has configured Tuma
        try:
            cfg = TenantTumaConfig.objects.get(schema_name=schema, is_active=True)
        except TenantTumaConfig.DoesNotExist:
            return Response(
                {'error': 'Payment gateway is not configured for this ISP.'}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        if not cfg.active_mode:
            return Response(
                {'error': 'No active payment collection mode set for this ISP.'}, 
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get or create Tuma payment method
        payment_method = self._get_or_create_tuma_payment_method()
        
        # Generate internal reference
        reference = f"PAY-{customer.customer_code}-{int(time.time())}"
        
        # Create payment record with proper model fields
        payment = Payment.objects.create(
            customer=customer,
            invoice=invoice,
            amount=Decimal(str(amount)),
            payment_method=payment_method,
            payer_phone=phone_number,
            mpesa_phone=phone_number,
            payment_reference=reference,
            status='PENDING',
            notes="Customer initiated payment via dashboard",
        )
        
        # Initiate Tuma STK Push
        try:
            client = TumaClient()
            
            # Authenticate using the CHILD credentials specific to this tenant
            token = client.auth_token(cfg.tuma_business_email, cfg.tuma_business_api_key)
            
            description = f"Account Recharge - {customer.full_name}"
            if invoice:
                description = f"Invoice #{invoice.invoice_number}"
            
            response = client.stk_push(
                token=token,
                amount=int(Decimal(str(amount))),
                phone=phone_number,
                callback_url=settings.TUMA_CALLBACK_URL,
                description=description
            )
            
            if response.get("success"):
                data = response.get("data", {})
                # Store Tuma tracking IDs
                payment.tuma_merchant_request_id = data.get("merchant_request_id", "")
                payment.tuma_checkout_request_id = data.get("checkout_request_id", "")
                payment.status = 'PROCESSING'
                payment.save()
                
                return Response({
                    'status': 'pending',
                    'payment_id': payment.id,
                    'tuma_response': {
                        'status': 'pending',
                        'merchant_request_id': payment.tuma_merchant_request_id,
                        'checkout_request_id': payment.tuma_checkout_request_id,
                        'message': 'STK Push sent to your phone. Please enter your PIN.',
                    }
                })
            else:
                payment.status = 'FAILED'
                payment.failure_reason = response.get("message", "Failed to initiate payment")
                payment.save()
                
                return Response({
                    'status': 'error',
                    'message': payment.failure_reason,
                }, status=status.HTTP_400_BAD_REQUEST)
        
        except TumaError as e:
            logger.error(f"Customer payment Tuma error: {str(e)}")
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
    Since Tuma operates purely via webhooks for resolution, we check our local database 
    status which is updated asynchronously by the TumaWebhookView.
    
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
        
        is_finalized = payment.status in ['COMPLETED', 'FAILED', 'CANCELLED']
        
        # Return current local DB status. 
        # (The webhook is responsible for updating this from PENDING/PROCESSING -> COMPLETED)
        return Response({
            'payment_id': payment.id,
            'status': payment.status.lower(),
            'message': self._get_status_message(payment),
            'mpesa_receipt': payment.mpesa_receipt,
            'amount': float(payment.amount),
            'completed_at': payment.processed_at,
            'outstanding_balance': float(customer.outstanding_balance or 0) if is_finalized else None,
        })
    
    def _get_status_message(self, payment):
        messages = {
            'COMPLETED': 'Payment successful!',
            'FAILED': payment.failure_reason or 'Payment failed',
            'CANCELLED': 'Payment was cancelled',
            'PENDING': 'Waiting for payment confirmation...',
            'PROCESSING': 'Processing payment... please check your phone.',
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
                'minimum_amount': float(method.minimum_amount),
                'maximum_amount': float(method.maximum_amount),
                'transaction_fee': float(method.transaction_fee),
                'fee_type': method.fee_type,
            }
            for method in methods
        ]
        
        return Response({
            'payment_methods': methods_data,
            'default_method': 'TUMA_STK',  # Updated default identifier
        })