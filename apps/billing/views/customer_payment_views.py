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

from apps.billing.models.payment_models import (
    Payment, 
    InvoiceItemPayment, 
    TenantTumaConfig,
    StkCancellationTracker   # ← NEW IMPORT
)
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
    
    def _validate_tuma_configuration(self, cfg):
        """
        Validate that the Tuma configuration has complete child business credentials.
        
        Returns:
            tuple: (is_valid, error_message)
        """
        if not cfg.is_active:
            return False, "Tuma payment gateway is not active for this ISP."
        
        if not cfg.tuma_business_id:
            return False, "Tuma business profile not created. Please contact support to complete setup."
        
        if not cfg.tuma_business_email:
            return False, "Tuma business email missing. Please reconfigure your payment settings."
        
        if not cfg.tuma_business_api_key:
            return False, "Tuma business API key missing. Please reconfigure your payment settings."
        
        if not cfg.active_mode:
            return False, "No active payment collection mode set (Till/Bank). Please configure your payment method."
        
        if not cfg.collection_account_number:
            return False, "Collection account number missing. Please reconfigure your payment settings."
        
        return True, None

    def _check_stk_cancellation_block(self, schema_name: str, phone_number: str):
        """
        Check if the user has been blocked due to consecutive STK cancellations (result_code 1032).
        Raises 429 Too Many Requests if blocked.
        """
        tracker = StkCancellationTracker.get_or_create_tracker(schema_name, phone_number)
        
        if tracker.is_currently_blocked() or tracker.consecutive_1032_count >= 3:
            logger.warning(
                f"STK Push blocked for phone {phone_number} under schema {schema_name}. "
                f"Consecutive 1032 count: {tracker.consecutive_1032_count}"
            )
            return Response(
                {
                    "error": "STK requests blocked due to multiple cancellations. "
                             "Please contact support to unblock your number.",
                    "detail": "You have cancelled the payment prompt too many times. "
                              "Try again later or contact support."
                },
                status=429  # Too Many Requests
            )
        return None  # No block

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

        # ====================== NEW: ANTI-ABUSE CHECK ======================
        # Block before STK Push if user has too many consecutive cancellations
        block_response = self._check_stk_cancellation_block(schema, phone_number)
        if block_response:
            return block_response
        # ===================================================================

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
        
        # ============================================================
        # CRITICAL: Validate Tuma configuration BEFORE initiating payment
        # ============================================================
        try:
            cfg = TenantTumaConfig.objects.get(schema_name=schema, is_active=True)
        except TenantTumaConfig.DoesNotExist:
            return Response(
                {'error': 'Payment gateway is not configured for this ISP. Please contact support.'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate configuration completeness
        is_valid, error_message = self._validate_tuma_configuration(cfg)
        if not is_valid:
            logger.error(f"Tuma configuration invalid for tenant {schema}: {error_message}")
            return Response(
                {'error': error_message},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get or create Tuma payment method
        payment_method = self._get_or_create_tuma_payment_method()
        
        # Generate internal reference
        reference = f"PAY-{customer.customer_code}-{int(time.time())}"
        
        # Create payment record
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
            schema_name=schema,
        )
        
        # ============================================================
        # Initiate Tuma STK Push using CHILD credentials
        # ============================================================
        try:
            client = TumaClient()
            
            logger.info(f"Initiating Tuma payment for tenant {schema} using business {cfg.tuma_business_id}")
            
            token = client.auth_token(cfg.tuma_business_email, cfg.tuma_business_api_key)
            
            description = f"Account Recharge - {customer.full_name}"
            if invoice:
                description = f"Invoice #{invoice.invoice_number}"
            
            if cfg.active_mode == "TILL":
                description = f"[Till] {description}"
            else:
                description = f"[Bank] {description}"
            
            response = client.stk_push(
                token=token,
                amount=int(Decimal(str(amount))),
                phone=phone_number,
                callback_url=settings.TUMA_CALLBACK_URL,
                description=description
            )
            
            if response.get("success"):
                data = response.get("data", {})
                payment.tuma_merchant_request_id = data.get("merchant_request_id", "")
                payment.tuma_checkout_request_id = data.get("checkout_request_id", "")
                payment.tuma_status = "pending"
                payment.status = 'PROCESSING'
                payment.save()
                
                logger.info(f"Tuma STK Push initiated successfully: {payment.tuma_merchant_request_id}")
                
                return Response({
                    'status': 'pending',
                    'payment_id': payment.id,
                    'payment_number': payment.payment_number,
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
                payment.tuma_status = "failed"
                payment.save()
                
                logger.error(f"Tuma STK Push failed: {payment.failure_reason}")
                
                return Response({
                    'status': 'error',
                    'message': payment.failure_reason,
                }, status=status.HTTP_400_BAD_REQUEST)
        
        except TumaError as e:
            logger.error(f"Customer payment Tuma error: {str(e)}")
            payment.status = 'FAILED'
            payment.failure_reason = str(e)
            payment.tuma_status = "failed"
            payment.save()
            
            return Response({
                'status': 'error',
                'message': 'Payment service unavailable. Please try again.',
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        except Exception as e:
            logger.exception(f"Unexpected error in customer payment: {str(e)}")
            payment.status = 'FAILED'
            payment.failure_reason = f"Unexpected error: {str(e)}"
            payment.save()
            
            return Response({
                'status': 'error',
                'message': 'An unexpected error occurred. Please try again.',
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
        
        return Response({
            'payment_id': payment.id,
            'payment_number': payment.payment_number,
            'status': payment.status.lower(),
            'tuma_status': payment.tuma_status,
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
            'default_method': 'TUMA_STK',
        })