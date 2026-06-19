from datetime import datetime
from decimal import Decimal
from django.utils import timezone
from django.conf import settings
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404
import json
import time
import logging

from ..permissions import CustomerOnlyPermission
from apps.billing.models import Invoice, Payment
from apps.billing.models.payment_models import InvoiceItemPayment
from apps.billing.serializers import InvoiceSerializer, PaymentSerializer

logger = logging.getLogger(__name__)


class PaymentView(APIView):
    """
    Customer payment operations - dynamically routes to Daraja or Tuma based on tenant config.
    
    POST /api/v1/self-service/payments/initiate/
    {
        "amount": 1000,
        "phone_number": "254712345678",
        "invoice_id": 123  // Optional
    }
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def get(self, request):
        """Get customer invoices and payments"""
        customer = request.user.customer_profile
        
        # Get invoices
        invoices = Invoice.objects.filter(customer=customer).order_by('-created_at')[:10]
        
        # Get payments
        payments = Payment.objects.filter(customer=customer).order_by('-created_at')[:10]
        
        # Get payment methods
        payment_methods = self._get_payment_methods()
        
        return Response({
            'invoices': [
                {
                    'id': inv.id,
                    'invoice_number': inv.invoice_number,
                    'amount': float(inv.amount),
                    'status': inv.status,
                    'due_date': inv.due_date,
                }
                for inv in invoices
            ],
            'payments': [
                {
                    'id': p.id,
                    'amount': float(p.amount),
                    'status': p.status,
                    'created_at': p.created_at,
                }
                for p in payments
            ],
            'payment_methods': payment_methods,
            'current_balance': float(getattr(customer, 'outstanding_balance', 0) or 0),
        })
    
    def post(self, request):
        """Initiate M-Pesa STK Push payment dynamically via Daraja or Tuma"""
        from django.db import connection
        user = request.user
        customer = user.customer_profile
        
        amount = request.data.get('amount')
        phone_number = request.data.get('phone_number')
        invoice_id = request.data.get('invoice_id')
        
        # 1. Validate Amount
        if not amount:
            return Response({'error': 'Amount is required. Please ensure the frontend is sending the amount.'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            amount = Decimal(str(amount))
            if amount <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return Response({'error': 'Invalid amount'}, status=status.HTTP_400_BAD_REQUEST)
        
        # 2. Validate Phone
        if not phone_number:
            phone_number = getattr(customer, 'phone_number', None) or getattr(user, 'phone_number', None)
            
        if not phone_number:
            return Response({'error': 'Phone number is required'}, status=status.HTTP_400_BAD_REQUEST)
            
        # 3. Get Invoice if specified
        invoice = None
        if invoice_id:
            try:
                invoice = Invoice.objects.get(id=invoice_id, customer=customer)
            except Invoice.DoesNotExist:
                return Response({'error': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)

        # 4. Get Active Payment Method for this Tenant (No PayHero!)
        payment_method = InvoiceItemPayment.objects.filter(
            schema_name=connection.schema_name, 
            is_active=True
        ).first()

        if not payment_method:
            return Response({
                'error': 'No active payment method configured. The ISP must set up M-Pesa or Tuma in the admin dashboard.'
            }, status=status.HTTP_400_BAD_REQUEST)

        # 5. Generate Reference & Create Payment
        reference = f"PAY-{customer.customer_code}-{int(time.time())}"
        full_name = getattr(customer, 'full_name', None) or f"{user.first_name} {user.last_name}".strip()
        description = f"Account Recharge - {full_name}"
        if invoice:
            description = f"Invoice #{invoice.invoice_number}"
        
        # ============================================================
        # FIX 1: Try to link the customer's current service plan amount
        # so the C2B webhook plan-matching works
        # ============================================================
        actual_amount = amount
        try:
            # Get the customer's active service with plan
            service = customer.services.filter(
                status__in=['ACTIVE', 'SUSPENDED'],
                plan__isnull=False
            ).first()
            
            if service and service.plan:
                # If the service has a plan, use the plan amount
                # This helps the C2B webhook match the payment to the right plan
                plan_amount = service.plan.price or service.plan.amount
                if plan_amount and plan_amount > 0:
                    # If the customer is paying exactly the plan amount, use it
                    # to ensure proper matching
                    if amount == plan_amount or amount >= plan_amount:
                        actual_amount = plan_amount
                        logger.info(f"Using plan amount {plan_amount} for C2B matching for customer {customer.customer_code}")
        except Exception as e:
            logger.warning(f"Could not get service plan for amount matching: {e}")
        
        # ============================================================
        # FIX 2: Create Payment with enhanced notes to identify it as 
        # a customer portal payment (per Claude's instruction)
        # ============================================================
        payment = Payment.objects.create(
            schema_name=connection.schema_name,
            customer=customer,
            invoice=invoice,
            amount=actual_amount,  # Use the actual amount (plan amount if matched)
            payment_method=payment_method,
            payer_phone=phone_number,
            mpesa_phone=phone_number,
            payment_reference=reference,
            status='PENDING',
            notes=f"Customer portal STK payment. Plan: {invoice.invoice_number if invoice else 'account recharge'}. Schema: {connection.schema_name}",
        )

        # ==========================================
        # ROUTE 1: DIRECT DARAJA (M-PESA CONFIG)
        # ==========================================
        if payment_method.mpesa_configuration and payment_method.mpesa_configuration.is_active:
            try:
                from apps.billing.integrations.mpesa_integration import MpesaSTKPush
                mpesa_service = MpesaSTKPush(config=payment_method.mpesa_configuration)
                
                result = mpesa_service.initiate_stk_push(
                    phone_number=phone_number,
                    amount=actual_amount,  # Use the actual amount
                    account_reference=reference,
                    transaction_desc=description,
                    payment=payment
                )
                
                if result['success']:
                    return Response({
                        'status': 'pending',
                        'payment_id': payment.id,
                        'checkout_request_id': result['data']['checkout_request_id'],
                        'message': 'Please check your phone and enter your PIN to complete payment'
                    })
                else:
                    payment.status = 'FAILED'
                    payment.failure_reason = result.get('message', 'Failed to initiate M-Pesa')
                    payment.save()
                    return Response({'error': payment.failure_reason}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                logger.error(f"Daraja initiation error: {e}")
                payment.status = 'FAILED'
                payment.failure_reason = str(e)
                payment.save()
                return Response({'error': 'Payment service unavailable. Please try again.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # ==========================================
        # ROUTE 2: TUMA GATEWAY
        # ==========================================
        elif payment_method.tuma_configuration and payment_method.tuma_configuration.is_active:
            try:
                from apps.billing.services.tuma_service import TumaClient, TumaError
                client = TumaClient()
                tuma_cfg = payment_method.tuma_configuration
                
                # Authenticate as the Child Business
                child_token = client.get_token(tuma_cfg.tuma_business_email, tuma_cfg.tuma_business_api_key)
                
                # Build the dynamic Webhook URL for this tenant
                sub_domain = connection.schema_name.replace('tenant_', '')
                callback_url = f"https://{sub_domain}.netily.co.ke/api/v1/billing/tuma/callback/"
                
                # Push to Tuma
                tuma_response = client.stk_push(
                    token=child_token,
                    amount=int(actual_amount),  # Use the actual amount
                    phone=phone_number,
                    callback_url=callback_url,
                    description=reference
                )
                
                checkout_id = tuma_response.get("data", {}).get("checkout_request_id", "")
                payment.tuma_merchant_request_id = tuma_response.get("data", {}).get("merchant_request_id", "")
                payment.tuma_checkout_request_id = checkout_id
                payment.transaction_id = checkout_id
                payment.status = 'PROCESSING'
                payment.save()
                
                return Response({
                    'status': 'pending',
                    'payment_id': payment.id,
                    'checkout_request_id': checkout_id,
                    'message': 'Please check your phone and enter your PIN to complete payment'
                })
                
            except Exception as e:
                logger.error(f"Tuma initiation error: {e}")
                payment.status = 'FAILED'
                payment.failure_reason = str(e)
                payment.save()
                return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
                
        else:
            return Response({'error': 'Payment method configuration is incomplete.'}, status=status.HTTP_400_BAD_REQUEST)
    
    def _get_payment_methods(self):
        """Get available payment methods"""
        return [
            {
                'id': 'mpesa',
                'name': 'M-Pesa',
                'icon': 'phone',
                'description': 'Pay via M-Pesa STK Push',
                'enabled': True,
            },
            {
                'id': 'card',
                'name': 'Credit/Debit Card',
                'icon': 'credit-card',
                'description': 'Coming soon',
                'enabled': False,
            },
            {
                'id': 'bank',
                'name': 'Bank Transfer',
                'icon': 'bank',
                'description': 'Coming soon',
                'enabled': False,
            },
        ]