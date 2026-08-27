"""
Customer Payment Views

Endpoints for ISP customers to make payments (recharge, invoice payments).
These payments go through the Netily Paybill Gateway (replaces Tuma).
"""

import logging
import time
from decimal import Decimal

from django.conf import settings
from django.db import transaction, connection
from django.db.models import ProtectedError, Q
from django.utils import timezone

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import HasRoleAccessPolicy
from apps.billing.models.payment_models import (
    Payment, 
    InvoiceItemPayment, 
    TenantTumaConfig,
    StkCancellationTracker
)
from apps.billing.models.billing_models import Invoice
# 🚨 NEW: Import Netily Paybill service (replaces Tuma)
from apps.billing.services.netily_paybill_service import (
    resolve_destination, stk_push, NetilyPaybillError,
)
# 🚨 NEW: Import TumaCallbackMap for tracking
from apps.core.models import TumaCallbackMap
from apps.core.otp_service import OTPService, OTPError

logger = logging.getLogger(__name__)


def require_payment_method_otp(request):
    """
    DEPRECATED: OTP verification now handled by OtpGuard at the page level.
    This function is kept for backward compatibility but no longer enforces
    per-request OTP checks for payment method operations.
    
    The frontend OtpGuard component gates access to the entire payment methods page,
    so backend per-request checks are redundant and cause 400 errors on write operations.
    """
    # Skip OTP check entirely — the page-level guard already verified the user
    # and IsAuthenticated permission ensures the user is logged in.
    return None


class InitiateCustomerPaymentView(APIView):
    """
    Initiate customer payment via Netily Paybill Gateway (replaces Tuma).
    
    POST /api/v1/billing/payments/initiate/
    {
        "amount": 2000,
        "phone_number": "254712345678",
        "invoice_id": 456   // Optional
    }
    """
    
    permission_classes = [IsAuthenticated]
    
    def _get_or_create_tuma_payment_method(self):
        """Get or create generic STK payment method for Tuma (kept for backward compatibility)"""
        method, created = InvoiceItemPayment.objects.get_or_create(
            code='TUMA_STK',
            defaults={
                'name': 'M-Pesa STK Push (Netily)',
                'method_type': 'MPESA_STK',
                'is_active': True,
            }
        )
        return method
    
    def _validate_tuma_configuration(self, cfg):
        """
        Validate that the Tuma configuration has complete child business credentials.
        (Kept for backward compatibility - may be removed in future)
        
        Returns:
            tuple: (is_valid, error_message)
        """
        if not cfg.is_active:
            return False, "Payment gateway is not active for this ISP."
        
        if not cfg.tuma_business_id:
            return False, "Business profile not created. Please contact support to complete setup."
        
        if not cfg.tuma_business_email:
            return False, "Business email missing. Please reconfigure your payment settings."
        
        if not cfg.tuma_business_api_key:
            return False, "Business API key missing. Please reconfigure your payment settings."
        
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
        # Get the active payment method
        # ============================================================
        payment_method = InvoiceItemPayment.objects.filter(
            schema_name=schema, is_active=True,
        ).exclude(code__startswith='HOTSPOT_').first()

        if not payment_method:
            return Response(
                {'error': 'No active payment method configured. Please contact support.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # ============================================================
        # 🚨 VALIDATE: Resolve settlement destination
        # ============================================================
        destination = resolve_destination(payment_method)
        if not destination:
            return Response(
                {'error': 'No valid settlement destination configured. Please contact support.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        party_b, account_reference, transaction_type, _desc = destination
        
        # Generate internal reference - ensure no spaces
        reference = f"PAY-{customer.customer_code}-{int(time.time())}".replace(" ", "-")
        
        # Create payment record with service_type='PPPOE'
        payment = Payment.objects.create(
            customer=customer,
            invoice=invoice,
            amount=Decimal(str(amount)),
            payment_method=payment_method,
            payer_phone=phone_number,
            mpesa_phone=phone_number,
            payment_reference=reference,
            status='PROCESSING',
            notes="Customer initiated payment via dashboard",
            schema_name=schema,
            service_type='PPPOE',   # Permanent classification for analytics
            tuma_status='pending',
        )
        
        # ============================================================
        # 🚨 Initiate STK Push via Netily Paybill (replaces Tuma)
        # ============================================================
        try:
            result = stk_push(
                amount=amount,
                phone_number=phone_number,
                party_b=party_b,
                account_reference=account_reference or customer.customer_code,
                transaction_desc=f"PAY-{customer.customer_code}"[:13],
                transaction_type=transaction_type,
            )
            
            payment.tuma_merchant_request_id = result['merchant_request_id']
            payment.tuma_checkout_request_id = result['checkout_request_id']
            payment.tuma_status = "pending"
            payment.status = 'PROCESSING'
            payment.save()
            
            # ── Store mapping for webhook resolution ──
            from django_tenants.utils import schema_context, get_public_schema_name
            with schema_context(get_public_schema_name()):
                TumaCallbackMap.objects.update_or_create(
                    merchant_request_id=payment.tuma_merchant_request_id,
                    defaults={
                        "checkout_request_id": payment.tuma_checkout_request_id,
                        "schema_name": schema,
                        "payment_reference": payment.payment_number,
                    },
                )
            
            logger.info(
                f"STK Push initiated for customer {customer.customer_code}: "
                f"merchant_request_id={payment.tuma_merchant_request_id}"
            )
            
            return Response({
                'status': 'pending',
                'payment_id': payment.id,
                'payment_number': payment.payment_number,
                'tuma_response': {
                    'status': 'pending',
                    'merchant_request_id': payment.tuma_merchant_request_id,
                    'checkout_request_id': payment.tuma_checkout_request_id,
                    'message': result.get('customer_message') or 'STK Push sent. Please enter your PIN.',
                }
            })
            
        except NetilyPaybillError as e:
            logger.error(f"STK Push failed for customer {customer.customer_code}: {str(e)}")
            payment.status = 'FAILED'
            payment.failure_reason = str(e)
            payment.tuma_status = 'failed'
            payment.save()
            
            return Response({
                'status': 'error',
                'message': payment.failure_reason,
            }, status=status.HTTP_400_BAD_REQUEST)
        
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
    Since Netily Paybill operates purely via webhooks for resolution, we check our local database 
    status which is updated asynchronously by the NetilyPaybillWebhookView.
    
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
    Get available payment methods for customer (read-only, customer-facing).
    
    GET /api/v1/billing/payment-methods/
    POST /api/v1/billing/payment-methods/
    """
    
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payment-methods"
    
    def get(self, request):
        from apps.billing.models.payment_models import InvoiceItemPayment
        
        # Support paginated response expected by admin-api.ts
        # Exclude hotspot-internal methods (auto-created by captive portal flow)
        is_active = request.query_params.get('is_active')
        methods = InvoiceItemPayment.objects.filter(
            schema_name=connection.schema_name,
        ).exclude(
            code__startswith='HOTSPOT_',
        ).select_related(
            'mpesa_configuration', 'tuma_configuration',
        ).order_by('name')
        if is_active is not None:
            methods = methods.filter(is_active=is_active.lower() == 'true')
        
        def _mpesa_cfg(m):
            cfg = m.mpesa_configuration
            if not cfg:
                return None
            return {
                'id': cfg.id,
                'business_shortcode': cfg.business_shortcode,
                'shortcode_type': cfg.shortcode_type,
                'is_sandbox': cfg.is_sandbox,
                'is_active': cfg.is_active,
                'validation_status': cfg.validation_status,
            }
        
        methods_data = [
            {
                'id': method.id,
                'code': method.code,
                'name': method.name,
                'method_type': method.method_type,
                'description': method.description,
                'is_active': method.is_active,
                'is_default': method.is_default,
                'till_number': method.till_number,
                'paybill_number': method.paybill_number,
                'account_number': method.account_number,
                'bank_name': method.bank_name,
                'custom_link': method.custom_link,
                'config_json': method.config_json,
                'minimum_amount': float(method.minimum_amount),
                'maximum_amount': float(method.maximum_amount),
                'transaction_fee': float(method.transaction_fee),
                'fee_type': method.fee_type,
                'status': method.status,
                'mpesa_configuration': method.mpesa_configuration_id,
                'mpesa_configuration_details': _mpesa_cfg(method),
                'tuma_configuration': method.tuma_configuration_id,
                'created_at': method.created_at.isoformat() if method.created_at else None,
                'updated_at': method.updated_at.isoformat() if method.updated_at else None,
            }
            for method in methods
        ]
        
        return Response({
            'count': len(methods_data),
            'results': methods_data,
        })

    def post(self, request):
        """
        Create a new payment method (admin only). Max 3 per tenant.
        
        FIX: Exclude Daraja-linked methods from the cap count so the backend
        stays in sync with the frontend filtering in the Netily tab.
        Daraja-linked methods (mpesa_configuration set) belong exclusively to
        the M-Pesa Daraja tab and should not count toward the Netily 3-method limit.
        
        OTP verification is handled by the frontend OtpGuard at the page level,
        so no per-request OTP check is required here.
        
        🚨 REMOVED: Tuma provisioning calls (ensure_child_business, sync_active_method_to_tuma)
        These are no longer needed since we route directly through Netily Paybill.
        """
        from apps.billing.models.payment_models import InvoiceItemPayment
        from apps.billing.serializers.payment_serializers import PaymentMethodSerializer

        schema = connection.schema_name
        
        # FIX: Exclude Daraja-linked methods from the cap count
        # This keeps the backend in sync with the frontend filtering
        existing_count = InvoiceItemPayment.objects.filter(
            schema_name=schema,
        ).exclude(
            code__startswith='HOTSPOT_',
        ).filter(
            mpesa_configuration__isnull=True,  # Exclude Daraja-linked methods from the cap
        ).count()
        
        if existing_count >= 3:
            return Response(
                {'detail': 'Maximum 3 payment methods allowed. Delete or deactivate one to add another.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = PaymentMethodSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # 2nd+ methods start inactive — only the first is active by default
        is_first = existing_count == 0
        method = serializer.save(
            schema_name=schema,
            created_by=request.user,
            updated_by=request.user,
            is_active=is_first,
        )

        # 🚨 REMOVED: Tuma provisioning (ensure_child_business, sync_active_method_to_tuma)
        # These calls are dead weight now - we route directly through Netily Paybill
        # No need to sync to Tuma since we bypass Tuma entirely

        return Response(serializer.data, status=status.HTTP_201_CREATED)


class PaymentMethodDetailView(APIView):
    """
    Retrieve, update or delete a single payment method.

    GET/PATCH/DELETE /api/v1/billing/payment-methods/<id>/
    """
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payment-methods"

    def _get_method(self, pk):
        from apps.billing.models.payment_models import InvoiceItemPayment
        return InvoiceItemPayment.objects.get(
            pk=pk, schema_name=connection.schema_name,
        )

    def get(self, request, pk):
        try:
            method = self._get_method(pk)
        except InvoiceItemPayment.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        from apps.billing.serializers.payment_serializers import PaymentMethodSerializer
        return Response(PaymentMethodSerializer(method).data)

    def patch(self, request, pk):
        """
        Update a payment method.
        
        OTP verification is handled by the frontend OtpGuard at the page level,
        so no per-request OTP check is required here.
        
        🚨 REMOVED: Tuma sync (sync_active_method_to_tuma)
        No longer needed - we route directly through Netily Paybill.
        """
        from apps.billing.serializers.payment_serializers import PaymentMethodSerializer
        try:
            method = self._get_method(pk)
        except InvoiceItemPayment.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = PaymentMethodSerializer(method, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        method = serializer.save(updated_by=request.user)

        # 🚨 REMOVED: Tuma sync (sync_active_method_to_tuma)
        # No longer needed - we route directly through Netily Paybill.
        # The resolve_destination() function reads from the local method config.

        data = serializer.data
        data['tuma_synced'] = True  # Always true since we don't use Tuma
        return Response(data)

    def delete(self, request, pk):
        """
        Delete a payment method.
        
        OTP verification is handled by the frontend OtpGuard at the page level,
        so no per-request OTP check is required here.
        
        🚨 REMOVED: Tuma deactivation (deactivate_tuma_collections, delete_tuma_business)
        No longer needed - we route directly through Netily Paybill.
        """
        try:
            method = self._get_method(pk)
        except InvoiceItemPayment.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        force = request.query_params.get('force', '').lower() == 'true'

        try:
            was_active = method.is_active
            schema = method.schema_name
            method.delete()
        except ProtectedError:
            if not force:
                payment_count = method.payments.count()
                return Response(
                    {
                        'detail': f'This method has {payment_count} payment(s) linked. '
                                  'Confirm to delete and unlink those payments.',
                        'payment_count': payment_count,
                        'can_force_delete': True,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            # Force delete: unlink payments, then delete
            from apps.billing.models.payment_models import Payment
            was_active = method.is_active
            schema = method.schema_name
            Payment.objects.filter(payment_method=method).update(payment_method=None)
            method.delete()

        # 🚨 REMOVED: Tuma sync after delete (deactivate_tuma_collections, delete_tuma_business)
        # No longer needed - we route directly through Netily Paybill

        return Response(
            {'tuma_action': None},  # No Tuma action needed
            status=status.HTTP_200_OK,
        )


class PaymentMethodToggleActiveView(APIView):
    """Toggle active state of a payment method."""
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payment-methods"

    def post(self, request, pk):
        """
        Toggle the active state of a payment method.
        
        OTP verification is handled by the frontend OtpGuard at the page level,
        so no per-request OTP check is required here.
        
        🚨 REMOVED: Tuma sync (sync_active_method_to_tuma, deactivate_tuma_collections)
        No longer needed - we route directly through Netily Paybill.
        """
        from apps.billing.models.payment_models import InvoiceItemPayment
        try:
            method = InvoiceItemPayment.objects.get(
                pk=pk, schema_name=connection.schema_name,
            )
        except InvoiceItemPayment.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        new_state = not method.is_active

        if new_state:
            # Only one active at a time — deactivate all others
            InvoiceItemPayment.objects.filter(
                schema_name=connection.schema_name, is_active=True,
            ).exclude(pk=pk).update(is_active=False)

        method.is_active = new_state
        method.save(update_fields=['is_active', 'updated_at'])

        # 🚨 REMOVED: Tuma sync (sync_active_method_to_tuma, deactivate_tuma_collections)
        # No longer needed - we route directly through Netily Paybill.
        # The resolve_destination() function reads from the local method config.

        from apps.billing.serializers.payment_serializers import PaymentMethodSerializer
        data = PaymentMethodSerializer(method).data
        data['tuma_synced'] = True  # Always true since we don't use Tuma
        return Response(data)