from datetime import datetime
from decimal import Decimal
from django.utils import timezone
from django.conf import settings
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404
from django.db import connection
import json
import time
import logging

from ..permissions import CustomerOnlyPermission
from apps.billing.models import Invoice, Payment
from apps.billing.models.payment_models import InvoiceItemPayment
from apps.billing.serializers import InvoiceSerializer, PaymentSerializer

# 🚨 NEW: Netily Paybill service imports (replaces Tuma)
from apps.billing.services.netily_paybill_service import (
    resolve_destination, stk_push, NetilyPaybillError,
)
from apps.core.models import TumaCallbackMap
from django_tenants.utils import schema_context, get_public_schema_name

logger = logging.getLogger(__name__)


class PaymentView(APIView):
    """
    Customer payment operations - dynamically routes to Daraja or Netily Paybill.
    
    POST /api/v1/self-service/payments/initiate/
    {
        "amount": 1000,
        "phone_number": "254712345678",
        "invoice_id": 123  // Optional
    }
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def _normalize_msisdn(self, value):
        """
        Normalize to Safaricom's required 2547XXXXXXXX format.
        
        Handles various input formats:
        - 0712345678 -> 254712345678
        - 712345678 -> 254712345678
        - +254712345678 -> 254712345678
        - 254712345678 -> 254712345678 (unchanged)
        - 0712-345-678 -> 254712345678 (strips non-digits)
        """
        # Remove all non-digit characters
        phone = ''.join(ch for ch in str(value or '') if ch.isdigit())
        
        # Normalize to 2547XXXXXXXX format
        if phone.startswith('0') and len(phone) == 10:
            # 0712345678 -> 254712345678
            phone = '254' + phone[1:]
        elif phone.startswith('7') and len(phone) == 9:
            # 712345678 -> 254712345678
            phone = '254' + phone
        elif phone.startswith('254') and len(phone) == 12:
            # 254712345678 -> unchanged
            pass
        elif phone.startswith('+254'):
            # +254712345678 -> 254712345678
            phone = phone[1:]
        # If it's already in correct format, leave as is
        
        return phone
    
    def _get_active_payment_method(self, schema_name):
        """
        Get the active payment method for the tenant.
        Excludes hotspot-internal methods.
        """
        return InvoiceItemPayment.objects.filter(
            schema_name=schema_name,
            is_active=True,
        ).exclude(code__startswith='HOTSPOT_').first()
    
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
        """Initiate M-Pesa STK Push payment dynamically via Daraja or Netily Paybill"""
        user = request.user
        customer = user.customer_profile
        schema = connection.schema_name
        
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
            
        # 3. Normalize phone
        normalized_phone = self._normalize_msisdn(phone_number)
        if len(normalized_phone) != 12 or not normalized_phone.startswith('254'):
            return Response({'error': f'Invalid phone number format: {phone_number}'}, status=status.HTTP_400_BAD_REQUEST)
        
        # 4. Get Invoice if specified
        invoice = None
        if invoice_id:
            try:
                invoice = Invoice.objects.get(id=invoice_id, customer=customer)
            except Invoice.DoesNotExist:
                return Response({'error': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)

        # 5. Get Active Payment Method (exclude hotspot-internal methods)
        payment_method = self._get_active_payment_method(schema)

        if not payment_method:
            return Response({
                'error': 'No active payment method configured. The ISP must set up a payment method in the admin dashboard.'
            }, status=status.HTTP_400_BAD_REQUEST)

        # 6. Generate Reference & Create Payment
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
        # a customer portal payment
        # ============================================================
        payment = Payment.objects.create(
            schema_name=schema,
            customer=customer,
            invoice=invoice,
            amount=actual_amount,  # Use the actual amount (plan amount if matched)
            payment_method=payment_method,
            payer_phone=normalized_phone,
            mpesa_phone=normalized_phone,
            payment_reference=reference,
            status='PROCESSING',
            notes=f"Customer portal STK payment. Plan: {invoice.invoice_number if invoice else 'account recharge'}. Schema: {schema}",
            service_type='PPPOE',  # Self-service payments are for PPPoE services
            tuma_status='pending',
        )

        # ==========================================
        # ROUTE 1: DIRECT DARAJA (M-PESA CONFIG)
        # ==========================================
        if payment_method.mpesa_configuration and payment_method.mpesa_configuration.is_active:
            try:
                from apps.billing.integrations.mpesa_integration import MpesaSTKPush
                mpesa_service = MpesaSTKPush(config=payment_method.mpesa_configuration)
                
                result = mpesa_service.initiate_stk_push(
                    phone_number=normalized_phone,
                    amount=actual_amount,  # Use the actual amount
                    account_reference=reference,
                    transaction_desc=description,
                    payment=payment
                )
                
                if result['success']:
                    payment.tuma_status = 'pending'
                    payment.save(update_fields=['tuma_status'])
                    
                    return Response({
                        'status': 'pending',
                        'payment_id': payment.id,
                        'checkout_request_id': result['data']['checkout_request_id'],
                        'message': 'Please check your phone and enter your PIN to complete payment'
                    })
                else:
                    payment.status = 'FAILED'
                    payment.tuma_status = 'failed'
                    payment.failure_reason = result.get('message', 'Failed to initiate M-Pesa')
                    payment.save(update_fields=['status', 'tuma_status', 'failure_reason'])
                    return Response({'error': payment.failure_reason}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                logger.error(f"Daraja initiation error: {e}")
                payment.status = 'FAILED'
                payment.tuma_status = 'failed'
                payment.failure_reason = str(e)
                payment.save(update_fields=['status', 'tuma_status', 'failure_reason'])
                return Response({'error': 'Payment service unavailable. Please try again.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # ==========================================
        # ROUTE 2: NETILY PAYBILL (Bank / Till / Paybill — no tenant API keys)
        # ==========================================
        else:
            destination = resolve_destination(payment_method)
            if not destination:
                payment.status = 'FAILED'
                payment.failure_reason = "No valid settlement destination configured for this payment method."
                payment.save(update_fields=['status', 'failure_reason'])
                return Response({'error': payment.failure_reason}, status=status.HTTP_400_BAD_REQUEST)

            party_b, account_reference, transaction_type, _desc = destination

            try:
                result = stk_push(
                    amount=actual_amount,
                    phone_number=normalized_phone,
                    party_b=party_b,
                    account_reference=account_reference or reference[:12],
                    transaction_desc=description[:13],
                    transaction_type=transaction_type,
                )
            except NetilyPaybillError as e:
                logger.error(f"Netily Paybill STK error for customer {customer.customer_code}: {e}")
                payment.status = 'FAILED'
                payment.tuma_status = 'failed'
                payment.failure_reason = str(e)
                payment.save(update_fields=['status', 'tuma_status', 'failure_reason'])
                return Response({'error': payment.failure_reason}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                logger.exception(f"Unexpected Netily Paybill error for customer {customer.customer_code}: {e}")
                payment.status = 'FAILED'
                payment.tuma_status = 'failed'
                payment.failure_reason = f"Unexpected error: {str(e)}"
                payment.save(update_fields=['status', 'tuma_status', 'failure_reason'])
                return Response(
                    {'error': 'Payment service unavailable. Please try again.'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            payment.tuma_merchant_request_id = result['merchant_request_id']
            payment.tuma_checkout_request_id = result['checkout_request_id']
            payment.transaction_id = result['checkout_request_id']
            payment.tuma_status = 'pending'
            payment.status = 'PROCESSING'
            payment.save(update_fields=[
                'tuma_merchant_request_id', 'tuma_checkout_request_id',
                'transaction_id', 'tuma_status', 'status',
            ])

            # Store mapping so the shared webhook can route the callback back to this schema
            with schema_context(get_public_schema_name()):
                TumaCallbackMap.objects.update_or_create(
                    merchant_request_id=payment.tuma_merchant_request_id,
                    defaults={
                        "checkout_request_id": payment.tuma_checkout_request_id,
                        "schema_name": schema,
                        "payment_reference": payment.payment_number,
                    },
                )

            return Response({
                'status': 'pending',
                'payment_id': payment.id,
                'checkout_request_id': payment.tuma_checkout_request_id,
                'message': result.get('customer_message') or 'Please check your phone and enter your PIN to complete payment',
            })
    
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