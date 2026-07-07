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
from django.db.models import ProtectedError
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
from apps.billing.services.tuma_service import TumaClient, TumaError
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
        
        # Generate internal reference - ensure no spaces
        reference = f"PAY-{customer.customer_code}-{int(time.time())}".replace(" ", "-")
        
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
            
            token = client.get_token(cfg.tuma_business_email, cfg.tuma_business_api_key)
            
            # Create a simple description with the payment reference (will be cleaned in service)
            description = f"PAY-{customer.customer_code}"
            
            # Pass the raw amount, the service will clean it (convert to integer)
            response = client.stk_push(
                token=token,
                amount=amount,  # Pass the raw amount, service will clean it
                phone=phone_number,
                callback_url=settings.TUMA_CALLBACK_URL,
                description=description  # Only 5 arguments now
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
        ).order_by('name')
        if is_active is not None:
            methods = methods.filter(is_active=is_active.lower() == 'true')
        
        methods_data = [
            {
                'id': method.id,
                'code': method.code,
                'name': method.name,
                'method_type': method.method_type,
                'description': method.description,
                'is_active': method.is_active,
                'is_default': method.is_default,
                # REMOVED: 'is_payhero_enabled': method.is_payhero_enabled,
                # REMOVED: 'channel_id': method.channel_id,
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
        
        OTP verification is handled by the frontend OtpGuard at the page level,
        so no per-request OTP check is required here.
        """
        from apps.billing.models.payment_models import InvoiceItemPayment
        from apps.billing.serializers.payment_serializers import PaymentMethodSerializer

        schema = connection.schema_name
        existing_count = InvoiceItemPayment.objects.filter(
            schema_name=schema,
        ).exclude(code__startswith='HOTSPOT_').count()
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

        # Auto-provision Tuma child business with real method data
        from apps.billing.services.tuma_service import (
            ensure_child_business, sync_active_method_to_tuma, TumaError,
        )
        try:
            cfg = ensure_child_business(schema, method=method)
            if not method.tuma_configuration:
                method.tuma_configuration = cfg
                method.save(update_fields=['tuma_configuration'])
            # First method is auto-active → sync its settlement details to Tuma
            if is_first:
                sync_active_method_to_tuma(schema, method)
        except TumaError as e:
            logger.warning(f"Tuma provisioning/sync failed for {schema}: {e}")

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
        """
        from apps.billing.serializers.payment_serializers import PaymentMethodSerializer
        try:
            method = self._get_method(pk)
        except InvoiceItemPayment.DoesNotExist:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = PaymentMethodSerializer(method, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        method = serializer.save(updated_by=request.user)

        # If the active method's settlement details changed, sync to Tuma (non-fatal)
        tuma_synced = False
        if method.is_active:
            from apps.billing.services.tuma_service import sync_active_method_to_tuma, TumaError
            try:
                result = sync_active_method_to_tuma(method.schema_name, method)
                tuma_synced = bool(result and result.get('tuma_synced'))
            except Exception as e:
                logger.warning(f"Tuma sync on update failed for {method.schema_name}: {e}")

        data = serializer.data
        data['tuma_synced'] = tuma_synced
        return Response(data)

    def delete(self, request, pk):
        """
        Delete a payment method.
        
        OTP verification is handled by the frontend OtpGuard at the page level,
        so no per-request OTP check is required here.
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

        # Tuma sync after delete
        from apps.billing.services.tuma_service import (
            deactivate_tuma_collections, delete_tuma_business, TumaError,
        )
        tuma_action = None
        remaining = InvoiceItemPayment.objects.filter(schema_name=schema).count()
        try:
            if remaining == 0:
                if delete_tuma_business(schema):
                    tuma_action = 'business_deleted'
            elif was_active:
                has_remaining_active = InvoiceItemPayment.objects.filter(
                    schema_name=schema, is_active=True,
                ).exists()
                if not has_remaining_active:
                    deactivate_tuma_collections(schema)
                    tuma_action = 'deactivated'
        except TumaError as e:
            logger.warning(f"Tuma sync after delete failed for {schema}: {e}")

        return Response(
            {'tuma_action': tuma_action},
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
        """
        from apps.billing.models.payment_models import InvoiceItemPayment
        from apps.billing.services.tuma_service import (
            sync_active_method_to_tuma, deactivate_tuma_collections, TumaError,
        )
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

        # Sync to Tuma — return detailed feedback but NEVER crash the toggle
        sync_details = {'tuma_synced': False}
        try:
            if new_state:
                result = sync_active_method_to_tuma(connection.schema_name, method)
                sync_details = result or {'tuma_synced': True}
            else:
                has_active = InvoiceItemPayment.objects.filter(
                    schema_name=connection.schema_name, is_active=True,
                ).exists()
                if not has_active:
                    try:
                        deactivate_tuma_collections(connection.schema_name)
                    except Exception as e:
                        logger.warning(f"Tuma deactivate failed (non-fatal): {e}")
                sync_details = {'tuma_synced': True, 'settlement_channel': 'None (all deactivated)'}
        except TumaError as e:
            logger.warning(f"Tuma sync on toggle failed for {connection.schema_name}: {e}")
            sync_details = {'tuma_synced': False, 'tuma_error': str(e)}
        except Exception as e:
            # Catch-all: Tuma being down (503, ConnectionError, etc.) must not prevent toggling
            logger.warning(f"Tuma sync unexpected error for {connection.schema_name}: {e}")
            sync_details = {'tuma_synced': False, 'tuma_error': str(e)}

        from apps.billing.serializers.payment_serializers import PaymentMethodSerializer
        data = PaymentMethodSerializer(method).data
        data.update(sync_details)
        return Response(data)
