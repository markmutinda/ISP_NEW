import requests
import base64
import json
import logging
from decimal import Decimal
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Q, Sum, Count

# Custom permissions
from ..models.payment_models import Payment
from apps.core.permissions import IsCompanyAdmin, IsCompanyStaff
from apps.customers.models import Customer
from ..models.billing_models import Invoice
# from ..models.payment_models import Payment, PaymentMethod, Receipt   # ← COMMENTED OUT to prevent circular/early import error
from ..serializers import (
    PaymentSerializer, PaymentMethodSerializer, ReceiptSerializer,
    PaymentCreateSerializer, PaymentDetailSerializer, MpesaSTKPushSerializer
)
from ..integrations.mpesa_integration import MpesaSTKPush, MpesaCallback, MpesaValidation
from ..integrations.africastalking import SMSService

logger = logging.getLogger(__name__)


# Temporarily disabled PaymentMethodViewSet to break import chain
# Uncomment after migrations succeed and models are loadable
# class PaymentMethodViewSet(viewsets.ModelViewSet):
#     """
#     ViewSet for managing payment methods (including PayHero channels)
#     """
#     queryset = PaymentMethod.objects.all()  # ← this line was causing the crash
#     serializer_class = PaymentMethodSerializer
#     permission_classes = [IsAuthenticated, IsCompanyAdmin]
#     filter_backends = [DjangoFilterBackend, filters.SearchFilter]
#     filterset_fields = ['method_type', 'is_active', 'status', 'is_payhero_enabled']
#     search_fields = ['name', 'code', 'description', 'channel_id']
#
#     def get_queryset(self):
#         user = self.request.user
#         if user.is_superuser:
#             return PaymentMethod.objects.all()
#         return PaymentMethod.objects.filter(company=user.company)
#
#     def perform_create(self, serializer):
#         serializer.save(
#             created_by=self.request.user,
#             company=self.request.user.company
#         )
#
#     @action(detail=True, methods=['post'])
#     def toggle_active(self, request, pk=None):
#         method = self.get_object()
#         method.is_active = not method.is_active
#         method.save()
#         return Response({'status': 'success', 'is_active': method.is_active})
#
#     @action(detail=True, methods=['post'])
#     def test_connection(self, request, pk=None):
#         method = self.get_object()
#
#         if method.method_type.startswith('MPESA') and not method.is_payhero_enabled:
#             mpesa = MpesaSTKPush(method.company)
#             token = mpesa._get_access_token()
#             return Response({
#                 'status': 'success' if token else 'error',
#                 'message': 'M-Pesa connection successful' if token else 'M-Pesa connection failed'
#             })
#
#         return Response({
#             'status': 'info',
#             'message': f'No test available for {method.method_type} {"(PayHero)" if method.is_payhero_enabled else ""}'
#         })


class PaymentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing payments with unified PayHero + fallback support
    """
    # queryset = Payment.objects.all()  # ← Avoid direct queryset here to prevent early model load
    permission_classes = [IsAuthenticated, IsCompanyStaff]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'payment_method', 'customer', 'is_reconciled']
    search_fields = [
        'payment_number', 'customer__customer_code', 'transaction_id',
        'mpesa_receipt', 'payhero_external_reference'
    ]
    ordering_fields = ['payment_date', 'amount', 'created_at']

    def get_queryset(self):
        user = self.request.user
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                # Use string model name if needed, but Payment is safe here
                return Payment.objects.filter(company_id=company_id)
            return Payment.objects.all()
        
        # Company users can only see payments from their company
        if hasattr(user, 'company') and user.company:
            queryset = Payment.objects.filter(company=user.company)
            
            # Customers can only see their own payments
            if hasattr(user, 'customer_profile'):
                return queryset.filter(customer=user.customer_profile)
            
            return queryset
        
        return Payment.objects.none()

    def get_serializer_class(self):
        if self.action == 'create':
            return PaymentCreateSerializer
        elif self.action == 'retrieve':
            return PaymentDetailSerializer
        return PaymentSerializer

    def perform_create(self, serializer):
        serializer.save(
            created_by=self.request.user,
            company=self.request.user.company
        )

    # === Standard Actions ===
    @action(detail=True, methods=['post'])
    def mark_completed(self, request, pk=None):
        payment = self.get_object()
        if payment.mark_as_completed(request.user):
            try:
                sms_service = SMSService(payment.company)
                sms_service.send_payment_confirmation(payment.customer, payment)
            except Exception:
                pass
            return Response({'status': 'success', 'message': 'Payment marked as completed'})
        return Response({'error': 'Cannot mark payment as completed'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def mark_failed(self, request, pk=None):
        payment = self.get_object()
        reason = request.data.get('reason', '')
        if payment.mark_as_failed(reason):
            return Response({'status': 'success', 'message': 'Payment marked as failed'})
        return Response({'error': 'Cannot mark payment as failed'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def reconcile(self, request, pk=None):
        payment = self.get_object()
        if not payment.is_reconciled:
            payment.is_reconciled = True
            payment.reconciled_at = timezone.now()
            payment.reconciled_by = request.user
            payment.save()
            return Response({'status': 'success', 'message': 'Payment reconciled'})
        return Response({'error': 'Payment already reconciled'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['post'])
    def refund(self, request, pk=None):
        payment = self.get_object()
        refund_amount = Decimal(request.data.get('refund_amount', 0)) or None
        refund_reason = request.data.get('refund_reason', '')

        refund_payment = payment.refund(refund_amount, refund_reason)
        if refund_payment:
            return Response({
                'status': 'success',
                'message': 'Refund processed',
                'refund_payment_id': refund_payment.id
            })
        return Response({'error': 'Cannot process refund'}, status=status.HTTP_400_BAD_REQUEST)

    # === PayHero Unified Initiation ===
    @action(detail=False, methods=['post'])
    def initiate(self, request):
        amount = Decimal(request.data.get('amount', 0))
        external_reference = request.data.get('external_reference')
        channel_id = request.data.get('channel_id')

        if amount <= 0 or not external_reference:
            return Response({'error': 'Invalid amount or external_reference'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Use safe string-based lookup or delayed import
            from ..models.payment_models import PaymentMethod
            if channel_id:
                method = PaymentMethod.objects.get(
                    company=request.user.company,
                    channel_id=channel_id,
                    is_active=True
                )
            else:
                method = PaymentMethod.objects.get(
                    company=request.user.company,
                    is_default=True,
                    is_active=True
                )
        except PaymentMethod.DoesNotExist:
            return Response({'error': 'No valid payment method found'}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            logger.error(f"Payment method lookup error: {str(e)}")
            return Response({'error': 'Error finding payment method'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        customer = getattr(request.user, 'customer_profile', None)
        payment = Payment.objects.create(
            company=request.user.company,
            customer=customer,
            amount=amount,
            payment_method=method,
            status='PENDING',
            payment_reference=external_reference,
            payhero_external_reference=external_reference if method.is_payhero_enabled else None,
            created_by=request.user
        )

        if method.is_payhero_enabled and method.channel_id:
            try:
                payload = {
                    "amount": int(amount),
                    "channel_id": method.channel_id,
                    "provider": "m-pesa" if "MPESA" in method.method_type else "bank",
                    "external_reference": external_reference,
                    "callback_url": settings.PAYHERO_CALLBACK_URL,
                }
                auth_str = f"{settings.PAYHERO_API_USERNAME}:{settings.PAYHERO_API_PASSWORD}"
                headers = {
                    'Authorization': f'Basic {base64.b64encode(auth_str.encode()).decode()}',
                    'Content-Type': 'application/json'
                }

                response = requests.post(
                    'https://api.payhero.co.ke/v1.1/payments/initiate',
                    json=payload,
                    headers=headers,
                    timeout=30
                )

                if response.status_code == 200:
                    resp_data = response.json()
                    return Response({
                        'status': 'success',
                        'message': 'Payment initiated via PayHero',
                        'payhero_response': resp_data,
                        'payment_id': payment.id
                    })
                else:
                    payment.status = 'FAILED'
                    payment.failure_reason = response.text[:500]
                    payment.save()
                    return Response({
                        'error': 'PayHero initiation failed',
                        'detail': response.text
                    }, status=status.HTTP_502_BAD_GATEWAY)

            except Exception as e:
                logger.error(f"PayHero initiation error: {str(e)}")
                payment.status = 'FAILED'
                payment.failure_reason = str(e)[:500]
                payment.save()
                return Response({'error': 'Gateway error'}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({
            'status': 'info',
            'message': 'Direct payment initiation not implemented in unified endpoint'
        })

    # === PayHero Callback ===
    @csrf_exempt
    @action(detail=False, methods=['post'], url_path='payhero/callback')
    def payhero_callback(self, request):
        data = request.data
        logger.info(f"PayHero callback received: {json.dumps(data, default=str)}")

        external_ref = data.get('external_reference')
        if not external_ref:
            return Response({'error': 'Missing external_reference'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                from ..models.payment_models import Payment
                payment = Payment.objects.select_for_update().get(
                    payhero_external_reference=external_ref
                )

                new_status = 'COMPLETED' if str(data.get('status', '')).lower() in ['success', 'completed'] else 'FAILED'
                payment.status = new_status
                payment.raw_callback = data
                payment.failure_reason = data.get('message', '') if new_status == 'FAILED' else ''
                payment.save()

                if new_status == 'COMPLETED':
                    payment.mark_as_completed()

            return Response({'status': 'ok'})

        except Payment.DoesNotExist:
            logger.warning(f"PayHero callback for unknown reference: {external_ref}")
            return Response({'error': 'Payment not found'}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            logger.error(f"PayHero callback processing error: {str(e)}")
            return Response({'error': 'Processing failed'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # === Legacy Direct M-Pesa ===
    @action(detail=False, methods=['post'])
    def mpesa_stk_push(self, request):
        serializer = MpesaSTKPushSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        customer_id = data.get('customer_id')
        invoice_id = data.get('invoice_id')
        amount = data.get('amount')
        phone_number = data.get('phone_number')
        
        try:
            customer = Customer.objects.get(id=customer_id)
        except Customer.DoesNotExist:
            return Response({'status': 'error', 'message': 'Customer not found'}, status=status.HTTP_404_NOT_FOUND)
        
        invoice = None
        if invoice_id:
            try:
                invoice = Invoice.objects.get(id=invoice_id, customer=customer)
            except Invoice.DoesNotExist:
                return Response({'status': 'error', 'message': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)
        
        is_valid_amount, amount_error = MpesaValidation.validate_amount(amount)
        if not is_valid_amount:
            return Response({'status': 'error', 'message': amount_error}, status=status.HTTP_400_BAD_REQUEST)
        
        is_valid_phone, formatted_phone, phone_error = MpesaValidation.validate_phone_number(phone_number)
        if not is_valid_phone:
            return Response({'status': 'error', 'message': phone_error}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            from ..models.payment_models import PaymentMethod
            payment_method = PaymentMethod.objects.get(
                method_type='MPESA_STK',
                company=request.user.company,
                is_active=True
            )
        except PaymentMethod.DoesNotExist:
            return Response({'status': 'error', 'message': 'M-Pesa payment method not configured'}, status=status.HTTP_400_BAD_REQUEST)
        
        from ..models.payment_models import Payment
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
            payment.status = 'FAILED'
            payment.failure_reason = result.get('message', 'STK Push failed')
            payment.save()
            return Response({
                'error': result.get('message', 'Failed to initiate STK Push'),
                'payment_id': payment.id
            }, status=status.HTTP_400_BAD_REQUEST)

    # ... rest of the file remains unchanged ...

    @action(detail=False, methods=['post'])
    def mpesa_callback(self, request):
        callback_data = request.data
        callback_handler = MpesaCallback()
        result = callback_handler.handle_stk_callback(callback_data)
        
        if result['status'] == 'SUCCESS':
            checkout_request_id = result['checkout_request_id']
            try:
                payment = Payment.objects.get(transaction_id=checkout_request_id, status='PENDING')
            except Payment.DoesNotExist:
                return Response({'status': 'error', 'message': 'Payment not found'}, status=status.HTTP_404_NOT_FOUND)
            
            transaction_data = result['transaction_data']
            payment.mpesa_receipt = transaction_data['mpesa_receipt']
            payment.mpesa_phone = transaction_data['phone_number']
            payment.payment_date = timezone.now()
            payment.mark_as_completed(payment.created_by)
            
            try:
                sms_service = SMSService(payment.company)
                sms_service.send_payment_confirmation(payment.customer, payment)
            except Exception:
                pass
            
            return Response({
                'status': 'success',
                'message': 'Payment processed successfully',
                'payment_id': payment.id,
                'receipt_number': payment.mpesa_receipt
            })
        else:
            checkout_request_id = result['checkout_request_id']
            try:
                payment = Payment.objects.get(transaction_id=checkout_request_id)
                payment.status = 'FAILED'
                payment.failure_reason = result.get('error_message', 'Transaction failed')
                payment.save()
            except Payment.DoesNotExist:
                pass
            return Response({
                'error': result.get('error_message', 'Transaction failed')
            }, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['post'])
    def bank_transfer(self, request):
        customer_id = request.data.get('customer_id')
        invoice_id = request.data.get('invoice_id')
        amount = Decimal(request.data.get('amount', 0))
        bank_name = request.data.get('bank_name')
        account_number = request.data.get('account_number')
        transaction_reference = request.data.get('transaction_reference')
        
        if amount <= 0:
            return Response({'status': 'error', 'message': 'Invalid amount'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            customer = Customer.objects.get(id=customer_id)
        except Customer.DoesNotExist:
            return Response({'status': 'error', 'message': 'Customer not found'}, status=status.HTTP_404_NOT_FOUND)
        
        invoice = None
        if invoice_id:
            try:
                invoice = Invoice.objects.get(id=invoice_id, customer=customer)
            except Invoice.DoesNotExist:
                return Response({'status': 'error', 'message': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)
        
        try:
            payment_method = PaymentMethod.objects.get(
                method_type='BANK_TRANSFER',
                company=request.user.company,
                is_active=True
            )
        except PaymentMethod.DoesNotExist:
            return Response({'status': 'error', 'message': 'Bank transfer payment method not configured'}, status=status.HTTP_400_BAD_REQUEST)
        
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

    # === Fixed: Dashboard Stats (No more 500 error) ===
    @action(detail=False, methods=['get'])
    def dashboard_stats(self, request):
        """Get payment dashboard statistics"""
        queryset = self.get_queryset()
        
        today = timezone.now().date()
        yesterday = today - timezone.timedelta(days=1)
        
        today_payments = queryset.filter(payment_date__date=today)
        today_count = today_payments.count()
        today_amount = today_payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        
        yesterday_payments = queryset.filter(payment_date__date=yesterday)
        yesterday_count = yesterday_payments.count()
        yesterday_amount = yesterday_payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        
        month_start = today.replace(day=1)
        month_payments = queryset.filter(payment_date__date__gte=month_start)
        month_count = month_payments.count()
        month_amount = month_payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        
        status_counts = {
            'PENDING': queryset.filter(status='PENDING').count(),
            'COMPLETED': queryset.filter(status='COMPLETED').count(),
            'FAILED': queryset.filter(status='FAILED').count(),
            'REFUNDED': queryset.filter(status='REFUNDED').count(),
        }
        
        method_distribution = queryset.values('payment_method__name').annotate(
            count=Count('id'),
            total=Sum('amount')
        ).order_by('-total')
        
        thirty_days_ago = today - timezone.timedelta(days=30)
        top_payers = queryset.filter(
            payment_date__date__gte=thirty_days_ago,
            status='COMPLETED'
        ).values('customer__customer_code', 'customer__user__first_name', 'customer__user__last_name').annotate(
            total_paid=Sum('amount'),
            payment_count=Count('id')
        ).order_by('-total_paid')[:10]
        
        stats = {
            'today': {'count': today_count, 'amount': today_amount},
            'yesterday': {'count': yesterday_count, 'amount': yesterday_amount},
            'this_month': {'count': month_count, 'amount': month_amount},
            'status_distribution': status_counts,
            'method_distribution': list(method_distribution),
            'top_payers': list(top_payers)
        }
        
        return Response(stats)

    def get_queryset(self):
        user = self.request.user
        
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return Payment.objects.filter(company_id=company_id)
            return Payment.objects.all()
        
        if hasattr(user, 'company') and user.company:
            queryset = Payment.objects.filter(company=user.company)
            if hasattr(user, 'customer_profile'):
                return queryset.filter(customer=user.customer_profile)
            return queryset
        
        return Payment.objects.none()


# class ReceiptViewSet(viewsets.ModelViewSet):
#     queryset = Receipt.objects.all()
#     serializer_class = ReceiptSerializer
#     permission_classes = [IsAuthenticated, IsCompanyStaff]
#     filter_backends = [DjangoFilterBackend, filters.SearchFilter]
#     filterset_fields = ['status', 'customer', 'company']
#     search_fields = ['receipt_number', 'customer__customer_code']
#
#     def get_queryset(self):
#         user = self.request.user
#         queryset = Receipt.objects.all()
#         if user.is_superuser:
#             return queryset
#         queryset = queryset.filter(company=user.company)
#         if getattr(user, 'role', None) == 'customer' and hasattr(user, 'customer_profile'):
#             return queryset.filter(customer=user.customer_profile)
#         return queryset
#
#     def perform_create(self, serializer):
#         serializer.save(
#             created_by=self.request.user,
#             company=self.request.user.company
#         )
#
#     @action(detail=True, methods=['post'])
#     def issue(self, request, pk=None):
#         receipt = self.get_object()
#         if receipt.issue_receipt(request.user):
#             return Response({'status': 'success', 'message': 'Receipt issued'})
#         return Response({'error': 'Cannot issue receipt'}, status=status.HTTP_400_BAD_REQUEST)
#
#     @action(detail=True, methods=['get'])
#     def download_pdf(self, request, pk=None):
#         receipt = self.get_object()
#         serializer = self.get_serializer(receipt)
#         return Response(serializer.data)
#
#     @action(detail=True, methods=['get'])
#     def share(self, request, pk=None):
#         receipt = self.get_object()
#         share_method = request.query_params.get('method', 'email')
#         if share_method == 'sms':
#             try:
#                 sms_service = SMSService(receipt.company)
#                 message = f"Receipt {receipt.receipt_number} for KES {receipt.amount} issued. Thank you!"
#                 result = sms_service.send_single_sms(
#                     receipt.customer.user.phone_number, message
#                 )
#                 if result['success']:
#                     return Response({'status': 'success', 'message': 'Receipt sent via SMS'})
#                 return Response({'error': 'Failed to send SMS'}, status=status.HTTP_400_BAD_REQUEST)
#             except Exception as e:
#                 return Response(
#                     {'error': str(e)},
#                     status=status.HTTP_500_INTERNAL_SERVER_ERROR
#                 )
#         return Response({'error': 'Invalid share method'}, status=status.HTTP_400_BAD_REQUEST)
#
#     def get_queryset(self):
#         user = self.request.user
#
#         if user.is_superuser:
#             company_id = self.request.query_params.get('company_id')
#             if company_id:
#                 return Receipt.objects.filter(company_id=company_id)
#             return Receipt.objects.all()
#
#         # Company users can only see receipts from their company
#         if hasattr(user, 'company') and user.company:
#             queryset = Receipt.objects.filter(company=user.company)
#
#             # Customers can only see their own receipts
#             if hasattr(user, 'customer_profile'):
#                 return queryset.filter(customer=user.customer_profile)
#
#             return queryset
#
#         return Receipt.objects.none()
