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
    Customer payment operations - delegates to PayHero for M-Pesa.
    
    POST /api/v1/self-service/payments/initiate/
    {
        "amount": 1000,
        "phone_number": "254712345678",
        "invoice_id": 123  // Optional
    }
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def _get_or_create_mpesa_payment_method(self):
        """Get or create M-Pesa STK payment method"""
        method, created = InvoiceItemPayment.objects.get_or_create(
            code='MPESA_STK',
            defaults={
                'name': 'M-Pesa STK Push',
                'method_type': 'MPESA_STK',
                'is_payhero_enabled': True,
                'is_active': True,
                'channel_id': getattr(settings, 'PAYHERO_CHANNEL_ID', 1180),
            }
        )
        return method
    
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
        """Initiate M-Pesa STK Push payment via PayHero"""
        user = request.user
        customer = user.customer_profile
        
        amount = request.data.get('amount')
        phone_number = request.data.get('phone_number')
        invoice_id = request.data.get('invoice_id')
        
        # Validate amount
        if not amount:
            return Response(
                {'error': 'Amount is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            amount = Decimal(str(amount))
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid amount'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if amount <= 0:
            return Response(
                {'error': 'Amount must be greater than 0'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate phone number
        if not phone_number:
            phone_number = getattr(customer, 'phone_number', None) or getattr(user, 'phone_number', None)
        
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
        
        # Get or create M-Pesa payment method
        payment_method = self._get_or_create_mpesa_payment_method()
        
        # Generate reference
        reference = f"PAY-{customer.customer_code}-{int(time.time())}"
        
        # Create payment record
        payment = Payment.objects.create(
            customer=customer,
            invoice=invoice,
            amount=amount,
            payment_method=payment_method,
            payer_phone=phone_number,
            mpesa_phone=phone_number,
            payment_reference=reference,
            status='PENDING',
            notes=f"Customer initiated payment via dashboard",
        )
        
        # Initiate PayHero STK Push
        try:
            from apps.billing.services.payhero import PayHeroClient, PayHeroError
            
            client = PayHeroClient()
            
            full_name = getattr(customer, 'full_name', None) or f"{user.first_name} {user.last_name}".strip()
            description = f"Account Recharge - {full_name}"
            if invoice:
                description = f"Invoice #{invoice.invoice_number}"
            
            response = client.stk_push(
                phone_number=phone_number,
                amount=int(amount),
                reference=reference,
                description=description,
                callback_url=settings.PAYHERO_BILLING_CALLBACK,
                channel_id=payment_method.channel_id,
            )
            
            if response.success:
                payment.transaction_id = response.checkout_request_id or ''
                payment.payhero_external_reference = reference
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
        
        except Exception as e:
            logger.error(f"Payment initiation error: {e}")
            payment.status = 'FAILED'
            payment.failure_reason = str(e)
            payment.save()
            
            return Response({
                'status': 'error',
                'message': 'Payment service unavailable. Please try again.',
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
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
