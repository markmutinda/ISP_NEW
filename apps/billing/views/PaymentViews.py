# apps/billing/views/PaymentViews.py
import requests
import base64
import json
import logging
from decimal import Decimal
from django.conf import settings
from django.core.cache import cache  # ← ADDED: For cross-process locking
from django.db import transaction, connection, IntegrityError  # ← ADDED: IntegrityError
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Q, Sum, Count, F
from django.db.models import ProtectedError

# Custom permissions
from ..models.payment_models import Payment
from apps.core.permissions import HasRoleAccessPolicy, IsCompanyAdmin, IsCompanyStaff
from apps.customers.models import Customer
from ..models.billing_models import Invoice
from ..serializers import (
    PaymentSerializer, PaymentMethodSerializer, ReceiptSerializer,
    PaymentCreateSerializer, PaymentDetailSerializer, MpesaSTKPushSerializer,
    MpesaConfigurationSerializer, MpesaConfigurationDetailSerializer,
    MpesaTransactionSerializer, MpesaConfigurationTestSerializer
)
from ..integrations.mpesa_integration import MpesaSTKPush, MpesaCallback, MpesaValidation
from ..integrations.africastalking import SMSService

logger = logging.getLogger(__name__)


# ==========================
# M-Pesa Configuration Views
# ==========================

class MpesaConfigurationViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing tenant-specific M-Pesa configurations
    """
    permission_classes = [IsAuthenticated, IsCompanyAdmin, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payment-methods"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active', 'is_default', 'is_sandbox', 'shortcode_type', 'c2b_urls_registered']
    search_fields = ['business_shortcode', 'shortcode_type']
    ordering_fields = ['created_at', 'updated_at', 'c2b_urls_registered_at']

    def get_queryset(self):
        """Return only configurations for the current tenant with stable ordering for pagination."""
        from ..models.payment_models import MpesaConfiguration
        
        user = self.request.user
        
        if user.is_superuser:
            schema_name = self.request.query_params.get('schema_name')
            if schema_name:
                qs = MpesaConfiguration.objects.filter(schema_name=schema_name)
            else:
                qs = MpesaConfiguration.objects.all()
        else:
            qs = MpesaConfiguration.objects.filter(schema_name=connection.schema_name)
        
        return qs.order_by('-updated_at', '-id')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return MpesaConfigurationDetailSerializer
        return MpesaConfigurationSerializer

    def get_permissions(self):
        if self.action == 'active_summary':
            return [IsAuthenticated(), IsCompanyStaff(), HasRoleAccessPolicy()]
        return super().get_permissions()

    def get_required_rbac_paths(self, request=None):
        if self.action == 'active_summary':
            return ("/admin/users", "/admin/payment-methods")
        return (self.required_rbac_path,)

    @action(detail=False, methods=['get'], url_path='active-summary')
    def active_summary(self, request):
        """Return non-secret active M-Pesa details needed by user workflows."""
        config = self.get_queryset().filter(is_active=True).first()
        if not config:
            return Response({"business_shortcode": None, "is_active": False})
        return Response({
            "id": config.id,
            "business_shortcode": config.business_shortcode,
            "shortcode_type": config.shortcode_type,
            "is_active": True,
        })

    # ============================================================
    # FIX: Auto-activate payment method on create
    # ============================================================
    def perform_create(self, serializer):
        """
        Create a new M-Pesa configuration for the current tenant.
        If the config is created active, auto-link/activate the payment method
        so hotspot/PPPoE checkout picks this gateway up immediately.
        """
        config = serializer.save(
            created_by=self.request.user,
            schema_name=connection.schema_name  # Always use active connection
        )
        
        # NEW: auto-link/activate the payment method so hotspot/PPPoE checkout
        # picks this gateway up immediately — no manual "Activate as Primary" needed.
        if config.is_active:
            self._sync_payment_method_for_config(config)

    # ============================================================
    # NEW: Shared helper method for payment method sync
    # ============================================================
    def _sync_payment_method_for_config(self, config):
        """
        Ensure an InvoiceItemPayment exists, is linked to this Daraja config,
        and is the sole active/default method for the tenant.
        Mirrors the logic in activate_as_primary() but is safe to call on create.
        """
        from ..models.payment_models import InvoiceItemPayment, TenantTumaConfig

        schema = connection.schema_name
        method_type = 'MPESA_STK' if config.shortcode_type == 'PAYBILL' else 'MPESA_TILL'

        # Reuse a method already linked to this config, if any
        method = InvoiceItemPayment.objects.filter(
            schema_name=schema, mpesa_configuration=config
        ).first()

        # Otherwise, reuse an existing M-Pesa-type method not tied to another gateway
        if not method:
            method = InvoiceItemPayment.objects.filter(
                schema_name=schema,
                method_type__in=['MPESA_STK', 'MPESA_TILL', 'MPESA_PAYBILL'],
                mpesa_configuration__isnull=True,
            ).first()

        # Otherwise create a fresh one
        if not method:
            method = InvoiceItemPayment.objects.create(
                schema_name=schema,
                mpesa_configuration=config,
                name=f'M-Pesa {config.shortcode_type} (Daraja)',
                code=f'DRJ_{config.business_shortcode}',
                method_type=method_type,
                is_active=True,
                is_default=True,
            )

        # Only one active method per tenant
        InvoiceItemPayment.objects.filter(
            schema_name=schema
        ).exclude(pk=method.pk).update(is_active=False, is_default=False)

        method.mpesa_configuration = config
        method.tuma_configuration = None
        method.is_active = True
        method.is_default = True
        method.save(update_fields=[
            'mpesa_configuration', 'tuma_configuration',
            'is_active', 'is_default', 'updated_at',
        ])

        TenantTumaConfig.objects.filter(schema_name=schema).update(is_active=False)

        return method

    @action(detail=True, methods=['post'])
    def test_connection(self, request, pk=None):
        """
        Test the M-Pesa configuration by attempting to get an access token.
        If test_phone and test_amount are provided, also performs an STK push test.
        """
        config = self.get_object()
        test_serializer = MpesaConfigurationTestSerializer(data=request.data)
        
        if not test_serializer.is_valid():
            return Response(test_serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        validated_data = test_serializer.validated_data
        phone = validated_data.get('test_phone')
        amount = validated_data.get('test_amount')
        
        try:
            # Initialize M-Pesa service with this configuration
            from ..integrations.mpesa_integration import MpesaSTKPush
            mpesa_service = MpesaSTKPush(config=config)
            
            # Test 1: Get access token
            token_result = mpesa_service.test_connection()
            
            if not token_result['success']:
                return Response({
                    'status': 'error',
                    'message': 'Connection test failed',
                    'details': token_result
                }, status=status.HTTP_400_BAD_REQUEST)

            # Default to token-only success unless STK test payload is provided.
            test_result = {
                'success': True,
                'message': 'Access token retrieved successfully (token-only test)'
            }

            # Test 2: Send test STK push only when both fields are provided.
            if phone and amount is not None:
                test_result = mpesa_service.initiate_stk_push(
                    phone_number=phone,
                    amount=amount,
                    account_reference="TEST",
                    transaction_desc="Test Transaction"
                )
            
            # Update last validated timestamp
            config.last_validated_at = timezone.now()
            config.validation_status = 'VALID' if test_result['success'] else 'INVALID'
            if not test_result['success']:
                config.validation_error = test_result.get('message', 'Test failed')
            config.save(update_fields=['last_validated_at', 'validation_status', 'validation_error'])
            
            return Response({
                'status': 'success' if test_result['success'] else 'error',
                'message': 'Configuration test completed',
                'token_test': token_result,
                'stk_test': {
                    'success': test_result['success'],
                    'message': test_result.get('message', ''),
                    'checkout_request_id': test_result.get('data', {}).get('checkout_request_id') if test_result['success'] else None
                },
                'mode': 'token_and_stk' if phone and amount is not None else 'token_only'
            })
            
        except Exception as e:
            logger.error(f"Error testing M-Pesa configuration {config.id}: {str(e)}")
            config.validation_status = 'INVALID'
            config.validation_error = str(e)[:255]
            config.save(update_fields=['validation_status', 'validation_error'])
            
            return Response({
                'status': 'error',
                'message': f'Test failed: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # ============================================================
    # NEW: Register URLs with Safaricom for C2B payments
    # ============================================================
    @action(detail=True, methods=['post'])
    def register_urls(self, request, pk=None):
        """
        Triggers the Register URL API call to Safaricom for this configuration.
        This tells Safaricom where to send payment notifications for Paybill/Till.
        """
        config = self.get_object()
        
        try:
            # Initialize M-Pesa service with this configuration
            from ..integrations.mpesa_integration import MpesaSTKPush
            mpesa_service = MpesaSTKPush(config=config)
            
            # Get optional custom URLs from request
            confirmation_url = request.data.get('confirmation_url')
            validation_url = request.data.get('validation_url')
            
            # Register URLs with Safaricom
            result = mpesa_service.register_c2b_urls(
                confirmation_url=confirmation_url,
                validation_url=validation_url
            )
            
            # Check if registration was successful
            response_data = result.get('data', result)
            if result.get('success', False) or response_data.get('ResponseCode') == '0' or response_data.get('errorCode') == '500.003.1001':
                # Update configuration with successful registration
                config.last_validated_at = timezone.now()
                config.validation_status = 'VALID'
                config.validation_error = ''
                # NEW: Mark URLs as registered
                config.c2b_urls_registered = True
                config.c2b_urls_registered_at = timezone.now()
                config.save(update_fields=[
                    'last_validated_at', 'validation_status', 'validation_error',
                    'c2b_urls_registered', 'c2b_urls_registered_at'
                ])
                
                return Response({
                    "status": "success",
                    "message": "URLs registered successfully with Safaricom",
                    "details": result.get('data', result)
                })
            else:
                # Update configuration with failed registration
                config.validation_status = 'INVALID'
                config.validation_error = result.get('message', 'URL registration failed')
                # Don't mark URLs as registered on failure
                config.save(update_fields=['validation_status', 'validation_error'])
                
                return Response({
                    "status": "error",
                    "message": result.get('message', 'Failed to register URLs'),
                    "details": result.get('data', result)
                }, status=status.HTTP_400_BAD_REQUEST)
                
        except Exception as e:
            logger.error(f"Error registering URLs for config {config.id}: {str(e)}")
            
            # Update configuration with error
            config.validation_status = 'INVALID'
            config.validation_error = str(e)[:255]
            config.save(update_fields=['validation_status', 'validation_error'])
            
            return Response({
                "status": "error",
                "message": f"Error registering URLs: {str(e)}"
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['post'])
    def set_default(self, request, pk=None):
        """
        Set this configuration as the default for the tenant
        """
        config = self.get_object()
        
        # Clear existing default
        from ..models.payment_models import MpesaConfiguration
        MpesaConfiguration.objects.filter(
            schema_name=config.schema_name,
            is_default=True
        ).exclude(pk=config.pk).update(is_default=False)
        
        # Set this as default
        config.is_default = True
        config.is_active = True  # Default should also be active
        config.save(update_fields=['is_default', 'is_active', 'updated_at'])
        
        return Response({
            'status': 'success',
            'message': 'Configuration set as default',
            'data': MpesaConfigurationSerializer(config).data
        })

    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """
        Toggle the active status of this configuration
        """
        config = self.get_object()
        config.is_active = not config.is_active
        config.save(update_fields=['is_active', 'updated_at'])
        
        return Response({
            'status': 'success',
            'is_active': config.is_active,
            'message': f'Configuration {"activated" if config.is_active else "deactivated"}'
        })

    @action(detail=False, methods=['get'])
    def active(self, request):
        """
        Get the active M-Pesa configuration for the current tenant
        """
        from ..models.payment_models import MpesaConfiguration
        
        config = MpesaConfiguration.get_active_configuration(connection.schema_name)
        
        if not config:
            return Response({
                'status': 'error',
                'message': 'No active M-Pesa configuration found for this tenant'
            }, status=status.HTTP_404_NOT_FOUND)
        
        serializer = MpesaConfigurationDetailSerializer(config)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def default(self, request):
        """
        Get the default M-Pesa configuration for the current tenant
        """
        from ..models.payment_models import MpesaConfiguration
        
        config = MpesaConfiguration.get_default_configuration(connection.schema_name)
        
        if not config:
            return Response({
                'status': 'error',
                'message': 'No default M-Pesa configuration found for this tenant'
            }, status=status.HTTP_404_NOT_FOUND)
        
        serializer = MpesaConfigurationDetailSerializer(config)
        return Response(serializer.data)

    # ============================================================
    # NEW: Daraja Gateway Activation/Deactivation
    # ============================================================
    
    @action(detail=True, methods=['post'])
    def activate_as_primary(self, request, pk=None):
        """
        Activate this Daraja config as the sole payment gateway.
        - Deactivates all Tuma configs
        - Links this config to the active InvoiceItemPayment
        - Enforces single active gateway
        - NOTE: Does NOT clear c2b_urls_registered from other configs
        """
        from ..models.payment_models import MpesaConfiguration, InvoiceItemPayment
        
        config = self.get_object()

        # 1. Deactivate all other MpesaConfigurations for STK only
        #    Preserve C2B registration awareness because Safaricom still has
        #    those URLs registered and will send C2B callbacks to them
        MpesaConfiguration.objects.filter(
            schema_name=connection.schema_name
        ).exclude(pk=config.pk).update(
            is_active=False,
            is_default=False
            # NOTE: c2b_urls_registered is intentionally NOT cleared here
            # because Safaricom still has those URLs registered and will
            # send C2B callbacks to them
        )

        config.is_active = True
        config.is_default = True
        config.save(update_fields=['is_active', 'is_default', 'updated_at'])

        # 2. Deactivate Tuma entirely
        from ..models.payment_models import TenantTumaConfig
        TenantTumaConfig.objects.filter(
            schema_name=connection.schema_name
        ).update(is_active=False)

        # 3. Use the shared helper to sync payment method
        method = self._sync_payment_method_for_config(config)

        # Check if other configs still have registered URLs (they'll still receive C2B)
        other_registered = MpesaConfiguration.objects.filter(
            schema_name=connection.schema_name,
            c2b_urls_registered=True,
        ).exclude(pk=config.pk).exists()

        logger.info(
            f"[{connection.schema_name}] Daraja activated as primary: "
            f"shortcode={config.business_shortcode}, method={method.code}, "
            f"c2b_others_registered={other_registered}"
        )

        return Response({
            'status': 'success',
            'message': 'Daraja activated. Tuma gateway has been deactivated.',
            'payment_method_id': method.id,
            'gateway': 'daraja',
            'c2b_still_active': other_registered,
            'c2b_note': 'Other Daraja configurations still have active C2B URL registrations at Safaricom.' if other_registered else None,
        })

    @action(detail=True, methods=['post'])
    def deactivate_daraja(self, request, pk=None):
        """
        Deactivate Daraja and restore Tuma as the gateway.
        """
        from ..models.payment_models import MpesaConfiguration, InvoiceItemPayment, TenantTumaConfig
        
        config = self.get_object()

        # 1. Deactivate this Daraja config
        config.is_active = False
        config.is_default = False
        config.save(update_fields=['is_active', 'is_default', 'updated_at'])

        # 2. Unlink from any InvoiceItemPayment
        InvoiceItemPayment.objects.filter(
            schema_name=connection.schema_name,
            mpesa_configuration=config
        ).update(is_active=False)

        # 3. Re-activate Tuma if it exists
        tuma_cfg = TenantTumaConfig.objects.filter(
            schema_name=connection.schema_name,
            tuma_business_id__isnull=False
        ).exclude(tuma_business_id='').first()

        tuma_restored = False
        if tuma_cfg:
            tuma_cfg.is_active = True
            tuma_cfg.save(update_fields=['is_active', 'updated_at'])
            # Re-activate the Tuma-linked payment method
            InvoiceItemPayment.objects.filter(
                schema_name=connection.schema_name,
                tuma_configuration=tuma_cfg
            ).update(is_active=True)
            tuma_restored = True

        # Check if other configs still have registered URLs (they'll still receive C2B)
        other_registered = MpesaConfiguration.objects.filter(
            schema_name=connection.schema_name,
            c2b_urls_registered=True,
        ).exclude(pk=config.pk).exists()

        return Response({
            'status': 'success',
            'message': 'Daraja deactivated.' + (' Tuma gateway restored.' if tuma_restored else ' No Tuma config found — configure Tuma in the Netily tab.'),
            'tuma_restored': tuma_restored,
            'gateway': 'tuma' if tuma_restored else 'none',
            'c2b_still_active': other_registered,
            'c2b_note': 'PPPoE customers can still pay via previously registered Paybill numbers.' if other_registered else None,
        })


# ==========================
# M-Pesa Transaction Views
# ==========================

class MpesaTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for viewing M-Pesa transactions (read-only)
    """
    permission_classes = [IsAuthenticated, IsCompanyStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payments"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'transaction_type', 'configuration']
    search_fields = [
        'merchant_request_id', 'checkout_request_id', 'transaction_id',
        'phone_number', 'account_reference'
    ]
    ordering_fields = ['created_at', 'amount']

    def get_queryset(self):
        """Return only transactions for the current tenant"""
        from ..models.payment_models import MpesaTransaction
        
        user = self.request.user
        
        if user.is_superuser:
            schema_name = self.request.query_params.get('schema_name')
            if schema_name:
                return MpesaTransaction.objects.filter(schema_name=schema_name)
            return MpesaTransaction.objects.all()
        
        return MpesaTransaction.objects.filter(schema_name=connection.schema_name)

    def get_serializer_class(self):
        from ..serializers import MpesaTransactionSerializer, MpesaTransactionDetailSerializer
        if self.action == 'retrieve':
            return MpesaTransactionDetailSerializer
        return MpesaTransactionSerializer

    @action(detail=True, methods=['get'])
    def status(self, request, pk=None):
        """
        Check the current status of a transaction
        """
        transaction = self.get_object()
        
        # If transaction is still pending, query M-Pesa for status
        if transaction.status == 'PENDING':
            try:
                from ..integrations.mpesa_integration import MpesaSTKPush
                mpesa_service = MpesaSTKPush(config=transaction.configuration)
                
                # Query transaction status
                status_result = mpesa_service.query_stk_status(
                    checkout_request_id=transaction.checkout_request_id
                )
                
                if status_result['success']:
                    result_data = status_result['data']
                    if result_data.get('ResultCode') == 0:
                        transaction.mark_completed(
                            transaction_id=result_data.get('TransactionID', ''),
                            callback_data=result_data
                        )
                    else:
                        transaction.mark_failed(
                            result_code=result_data.get('ResultCode'),
                            result_desc=result_data.get('ResultDesc', 'Transaction failed'),
                            callback_data=result_data
                        )
            except Exception as e:
                logger.error(f"Error querying transaction status: {str(e)}")
        
        serializer = self.get_serializer(transaction)
        return Response(serializer.data)


# ==========================
# Payment Method Views (Updated - Tuma Removed)
# ==========================

class PaymentMethodViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing payment methods (M-Pesa and Bank Transfers only)
    
    🚨 UPDATED: Removed all Tuma provisioning/sync calls.
    Payment methods now route directly through Netily Paybill.
    """
    permission_classes = [IsAuthenticated, IsCompanyAdmin, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payment-methods"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['method_type', 'is_active', 'status']
    search_fields = ['name', 'code', 'description']

    def get_queryset(self):
        from ..models.payment_models import InvoiceItemPayment
        
        user = self.request.user
        if user.is_superuser:
            return InvoiceItemPayment.objects.all()
        
        # Use connection.schema_name for tenant safety
        return InvoiceItemPayment.objects.filter(schema_name=connection.schema_name)

    def get_serializer_class(self):
        return PaymentMethodSerializer

    def perform_create(self, serializer):
        """
        Create a new payment method.
        
        🚨 REMOVED: Tuma provisioning (ensure_child_business, sync_active_method_to_tuma)
        These calls are dead weight now - we route directly through Netily Paybill.
        The resolve_destination() function reads from the local method config.
        """
        from ..models.payment_models import InvoiceItemPayment
        user = self.request.user
        schema = connection.schema_name

        # ============================================================
        # FIX: Exclude Daraja-linked methods from the cap count
        # This keeps the backend in sync with the frontend filtering
        # Daraja-linked methods (mpesa_configuration set) belong exclusively to
        # the M-Pesa Daraja tab and should not count toward the 3-method limit.
        # ============================================================
        existing_count = InvoiceItemPayment.objects.filter(
            schema_name=schema,
        ).filter(
            mpesa_configuration__isnull=True,  # Exclude Daraja-linked methods from the cap
        ).count()
        
        if existing_count >= 3:
            from rest_framework.exceptions import ValidationError
            raise ValidationError('Maximum 3 payment methods allowed. Delete or deactivate one to add another.')

        # 2nd+ methods start inactive — only the first is active by default
        is_first = existing_count == 0
        method = serializer.save(
            created_by=user,
            schema_name=schema,
            is_active=is_first,
        )

        # 🚨 REMOVED: Tuma provisioning (ensure_child_business, sync_active_method_to_tuma)
        # These calls are dead weight now - we route directly through Netily Paybill
        # No need to sync to Tuma since we bypass Tuma entirely

    def perform_update(self, serializer):
        """
        Update a payment method.
        
        🚨 REMOVED: Tuma sync (sync_active_method_to_tuma)
        No longer needed - we route directly through Netily Paybill.
        The resolve_destination() function reads from the local method config.
        """
        method = serializer.save()
        # 🚨 REMOVED: Tuma sync - no longer needed

    def destroy(self, request, *args, **kwargs):
        """
        Delete a payment method.
        
        🚨 REMOVED: Tuma deactivation (deactivate_tuma_collections, delete_tuma_business)
        No longer needed - we route directly through Netily Paybill.
        """
        method = self.get_object()
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
            from ..models.payment_models import Payment
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

    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """
        Toggle the active state of a payment method.
        
        🚨 REMOVED: Tuma sync (sync_active_method_to_tuma, deactivate_tuma_collections)
        No longer needed - we route directly through Netily Paybill.
        """
        from ..models.payment_models import InvoiceItemPayment
        method = self.get_object()
        new_state = not method.is_active

        if new_state:
            # Only one active at a time — deactivate all others for this tenant
            InvoiceItemPayment.objects.filter(
                schema_name=connection.schema_name, is_active=True,
            ).exclude(pk=method.pk).update(is_active=False)

        method.is_active = new_state
        method.save()

        # 🚨 REMOVED: Tuma sync (sync_active_method_to_tuma, deactivate_tuma_collections)
        # No longer needed - we route directly through Netily Paybill.
        # The resolve_destination() function reads from the local method config.

        return Response({
            'status': 'success',
            'is_active': method.is_active,
            'tuma_synced': True,  # Always true since we don't use Tuma
        })

    @action(detail=True, methods=['post'])
    def test_connection(self, request, pk=None):
        """Test connection for M-Pesa payment methods only (Tuma removed)"""
        method = self.get_object()

        # Test M-Pesa connection if it's an M-Pesa method
        if method.method_type.startswith('MPESA'):
            if not method.mpesa_configuration:
                return Response({
                    'status': 'error',
                    'message': 'No M-Pesa configuration linked to this payment method'
                }, status=status.HTTP_400_BAD_REQUEST)
            
            try:
                from ..integrations.mpesa_integration import MpesaSTKPush
                mpesa = MpesaSTKPush(config=method.mpesa_configuration)
                token_result = mpesa.test_connection()
                
                return Response({
                    'status': 'success' if token_result['success'] else 'error',
                    'message': 'M-Pesa connection successful' if token_result['success'] else 'M-Pesa connection failed',
                    'details': token_result
                })
            except Exception as e:
                logger.error(f"Error testing M-Pesa connection: {str(e)}")
                return Response({
                    'status': 'error',
                    'message': f'M-Pesa connection test failed: {str(e)}'
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            'status': 'info',
            'message': f'No test available for {method.method_type}'
        })


# ==========================
# Payment Views (Updated - Tuma Removed)
# ==========================

class PaymentViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing payments with M-Pesa support (Tuma removed)
    """
    permission_classes = [IsAuthenticated, IsCompanyStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payments"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'payment_method', 'customer', 'is_reconciled']
    search_fields = [
        'payment_number', 'customer__customer_code', 'transaction_id',
        'mpesa_receipt'
    ]
    ordering_fields = ['payment_date', 'amount', 'created_at']

    def get_permissions(self):
        """Override permissions for specific actions - CRITICAL for M-Pesa callback"""
        if self.action == 'mpesa_callback':
            # No authentication required for Safaricom callback
            return []
        return super().get_permissions()

    def get_queryset(self):
        from ..models.payment_models import Payment
        
        user = self.request.user
        
        if user.is_superuser:
            # For superusers, optionally filter by schema_name
            schema_name = self.request.query_params.get('schema_name')
            if schema_name:
                return Payment.objects.filter(schema_name=schema_name)
            return Payment.objects.all()
        
        # FIX: Filter to only show COMPLETED payments in the list view
        # This ensures the frontend getPayments call only returns successful payments
        queryset = Payment.objects.filter(
            schema_name=connection.schema_name,
            status='COMPLETED'
        )
        
        if hasattr(user, 'customer_profile'):
            return queryset.filter(customer=user.customer_profile)
        
        return queryset

    def get_serializer_class(self):
        if self.action == 'create':
            return PaymentCreateSerializer
        elif self.action == 'retrieve':
            return PaymentDetailSerializer
        return PaymentSerializer

    def perform_create(self, serializer):
        user = self.request.user
        
        serializer.save(
            created_by=user,
            schema_name=connection.schema_name
        )

    # === Standard Actions ===
    @action(detail=True, methods=['post'])
    def mark_completed(self, request, pk=None):
        payment = self.get_object()
        if payment.mark_as_completed(request.user):
            # SMS BLOCK REMOVED - Dead code cleanup
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

    # === M-Pesa STK Push with Tenant Configuration ===
    @action(detail=False, methods=['post'])
    def mpesa_stk_push(self, request):
        serializer = MpesaSTKPushSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        customer_id = data.get('customer_id')
        invoice_id = data.get('invoice_id')
        service_connection_id = data.get('service_connection_id')
        amount = data.get('amount')
        phone_number = data.get('phone_number')
        account_reference = data.get('account_reference')
        transaction_desc = data.get('transaction_desc', 'Payment for Internet Services')
        
        # Get customer
        try:
            customer = Customer.objects.get(id=customer_id)
        except Customer.DoesNotExist:
            return Response({'status': 'error', 'message': 'Customer not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Get invoice if provided
        invoice = None
        if invoice_id:
            try:
                invoice = Invoice.objects.get(id=invoice_id, customer=customer)
            except Invoice.DoesNotExist:
                return Response({'status': 'error', 'message': 'Invoice not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Get service connection for account reference if needed
        service_connection = None
        if service_connection_id:
            try:
                from apps.customers.models import ServiceConnection
                service_connection = ServiceConnection.objects.get(
                    id=service_connection_id, 
                    customer=customer
                )
            except ServiceConnection.DoesNotExist:
                return Response({'status': 'error', 'message': 'Service connection not found'}, status=status.HTTP_404_NOT_FOUND)
        
        # Get M-Pesa payment method
        from ..models.payment_models import InvoiceItemPayment
        try:
            payment_method = InvoiceItemPayment.objects.get(
                method_type='MPESA_STK',
                schema_name=connection.schema_name,
                is_active=True
            )
        except InvoiceItemPayment.DoesNotExist:
            return Response({
                'status': 'error', 
                'message': 'M-Pesa STK payment method not configured for this company'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Check if payment method has M-Pesa configuration
        if not payment_method.mpesa_configuration or not payment_method.mpesa_configuration.is_active:
            return Response({
                'status': 'error',
                'message': 'M-Pesa service is not properly configured. Please contact support.'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # FIX 4: Create payment with service_type='PPPOE'
        payment = Payment.objects.create(
            schema_name=connection.schema_name,
            customer=customer,
            invoice=invoice,
            amount=amount,
            payment_method=payment_method,
            status='PENDING',
            payer_phone=phone_number,
            payer_name=customer.full_name,
            created_by=request.user,
            service_type='PPPOE',   # Permanent classification for analytics
        )
        
        # Determine account reference
        if not account_reference:
            if service_connection:
                account_reference = service_connection.effective_mpesa_account
            elif invoice:
                account_reference = invoice.invoice_number
            else:
                account_reference = customer.customer_code
        
        # Initialize M-Pesa service with tenant configuration
        from ..integrations.mpesa_integration import MpesaSTKPush
        mpesa_service = MpesaSTKPush(config=payment_method.mpesa_configuration)
        
        # Initiate STK push
        result = mpesa_service.initiate_stk_push(
            phone_number=phone_number,
            amount=amount,
            account_reference=account_reference,
            transaction_desc=transaction_desc,
            payment=payment  # Pass payment to link transaction
        )
        
        if result['success']:
            return Response({
                'status': 'success',
                'message': result['message'],
                'payment_id': payment.id,
                'checkout_request_id': result['data']['checkout_request_id'],
                'customer_message': 'Please check your phone and enter your PIN to complete payment'
            })
        else:
            payment.status = 'FAILED'
            payment.failure_reason = result.get('message', 'STK Push failed')
            payment.save()
            
            return Response({
                'status': 'error',
                'message': result.get('message', 'Failed to initiate STK Push'),
                'payment_id': payment.id
            }, status=status.HTTP_400_BAD_REQUEST)

    # === M-Pesa Callback Handler ===
    @csrf_exempt
    @action(
        detail=False, 
        methods=['post'], 
        url_path='mpesa/callback',
        permission_classes=[AllowAny],        # ← ADDED: Allow any access for Safaricom callback
        authentication_classes=[],             # ← ADDED: Disable authentication for this endpoint
    )
    def mpesa_callback(self, request):
        """
        Handle M-Pesa callbacks from Safaricom
        """
        callback_data = request.data
        logger.info(f"M-Pesa callback received: {json.dumps(callback_data, default=str)}")
        
        from ..integrations.mpesa_integration import MpesaCallback
        callback_handler = MpesaCallback()
        result = callback_handler.handle_stk_callback(callback_data)
        
        # ADD THIS: Handle failed callbacks too
        if result['status'] == 'FAILED':
            checkout_request_id = result.get('checkout_request_id')
            if checkout_request_id:
                from ..models.payment_models import MpesaTransaction, Payment
                try:
                    mpesa_txn = MpesaTransaction.objects.filter(
                        checkout_request_id=checkout_request_id
                    ).first()
                    if mpesa_txn:
                        mpesa_txn.mark_failed(
                            result_code=result.get('result_code', 1),
                            result_desc=result.get('result_desc', 'Transaction failed'),
                            callback_data=callback_data
                        )
                        if mpesa_txn.payment:
                            mpesa_txn.payment.status = 'FAILED'
                            mpesa_txn.payment.failure_reason = result.get('result_desc', 'STK Push failed or cancelled')
                            mpesa_txn.payment.save(update_fields=['status', 'failure_reason'])
                except Exception as e:
                    logger.error(f"Error handling failed STK callback: {e}")
        
        if result['status'] == 'SUCCESS':
            checkout_request_id = result['checkout_request_id']
            
            # Find the payment via MpesaTransaction
            from ..models.payment_models import MpesaTransaction, Payment
            try:
                mpesa_transaction = MpesaTransaction.objects.get(
                    checkout_request_id=checkout_request_id
                )
                
                # ── IDEMPOTENCY CHECK ──────────────────────────────────────
                # If already completed, return success immediately (Safaricom retries)
                if mpesa_transaction.status == 'COMPLETED':
                    logger.info(f"Duplicate callback ignored for {checkout_request_id}")
                    return Response({'ResultCode': 0, 'ResultDesc': 'Already processed'})
                # ──────────────────────────────────────────────────────────
                
                # 🧠 Fixed typo: changed payment_record to payment attribute
                payment = mpesa_transaction.payment if hasattr(mpesa_transaction, 'payment') else None
                if payment:
                    # Check payment not already completed
                    if payment.status == 'COMPLETED':
                        logger.info(f"Payment already completed for {checkout_request_id}")
                        return Response({'ResultCode': 0, 'ResultDesc': 'Already processed'})
                    
                    transaction_data = result['transaction_data']
                    
                    # ── NEW: Get receipt before doing any work ──
                    mpesa_receipt = transaction_data.get('mpesa_receipt')
                    
                    # ── NEW: cross-process lock ──
                    lock_key = f"mpesa_receipt_lock:{mpesa_receipt}" if mpesa_receipt else None
                    if lock_key and not cache.add(lock_key, "1", timeout=25):
                        logger.info(
                            f"STK callback: receipt {mpesa_receipt} already being processed "
                            "(likely the matching C2B confirmation), skipping duplicate"
                        )
                        return Response({'ResultCode': 0, 'ResultDesc': 'Already processing'})
                    
                    try:
                        # ── NEW: Belt-and-braces check before touching Payment ──
                        # Guard against a C2B-created MpesaTransaction already
                        # owning this receipt before we touch the Payment at all
                        if mpesa_receipt and MpesaTransaction.objects.filter(
                            transaction_id=mpesa_receipt
                        ).exclude(pk=mpesa_transaction.pk).exists():
                            logger.warning(
                                f"Receipt {mpesa_receipt} already recorded by another "
                                f"MpesaTransaction (C2B race) — skipping STK duplicate processing"
                            )
                            return Response({'ResultCode': 0, 'ResultDesc': 'Already processed via C2B'})
                        
                        # Update payment with M-Pesa details
                        payment.mpesa_receipt = mpesa_receipt
                        payment.mpesa_phone = transaction_data['phone_number']
                        payment.transaction_id = mpesa_receipt
                        payment.payment_date = timezone.now()
                        payment.mark_as_completed()

                        # ============================================================
                        # FIX: PPPoE Subscription Renewal (Customer Portal STK payments)
                        # Added per Claude's instruction - handles PPPoE renewal for
                        # customer-portal-initiated STK payments
                        # ============================================================
                        if payment.customer and not getattr(payment, 'hotspot_session', None):
                            try:
                                from apps.radius.models import CustomerRadiusCredentials
                                from django.utils import timezone as _tz
                                
                                creds = CustomerRadiusCredentials.objects.filter(
                                    customer=payment.customer
                                ).first()
                                
                                service = payment.customer.services.filter(
                                    status__in=['ACTIVE', 'SUSPENDED'],
                                    plan__isnull=False
                                ).first()
                                
                                if creds and service and service.plan:
                                    plan = service.plan
                                    now = _tz.now()
                                    validity_delta = plan.get_validity_timedelta()
                                    
                                    if validity_delta is None:
                                        new_expiry = None
                                    else:
                                        current_expiry = creds.expiration_date
                                        if current_expiry and current_expiry > now:
                                            new_expiry = current_expiry + validity_delta
                                        else:
                                            new_expiry = now + validity_delta
                                    
                                    creds.expiration_date = new_expiry
                                    creds.is_enabled = True
                                    creds.subscription_activated_at = now
                                    creds.save(update_fields=['expiration_date', 'is_enabled', 'subscription_activated_at'])
                                    creds.sync_to_radius()
                                    
                                    if service.status == 'SUSPENDED':
                                        service.status = 'ACTIVE'
                                        service.save(update_fields=['status'])
                                    
                                    try:
                                        from apps.messaging.services.notification_sender import SMSNotifier
                                        SMSNotifier.pppoe_renewal(
                                            customer=payment.customer,
                                            plan_name=plan.name,
                                            expires_at=new_expiry,
                                            reference=payment.mpesa_receipt or '',
                                            schema_name=connection.schema_name,
                                        )
                                    except Exception as sms_err:
                                        logger.warning(f"Renewal SMS failed: {sms_err}")
                                    
                                    try:
                                        from apps.radius.services.coa_service import CoAService
                                        coa = CoAService()
                                        router_ip = (
                                            creds.router.vpn_ip_address or creds.router.ip_address
                                            if creds.router else None
                                        )
                                        if router_ip:
                                            coa.disconnect_user(username=creds.username, nas_ip_address=router_ip)
                                    except Exception:
                                        pass
                                    
                                    logger.info(f"PPPoE renewed via STK for {payment.customer.customer_code}, new expiry: {new_expiry}")
                            except Exception as pppoe_err:
                                logger.error(f"PPPoE renewal failed for payment {payment.payment_number}: {pppoe_err}")

                        # ============================================================
                        # FIX: Propagate completion to linked HotspotSession (Daraja hotspot flow)
                        # ============================================================
                        try:
                            from apps.billing.models.hotspot_models import HotspotSession
                            hotspot_session = (
                                HotspotSession.objects
                                .filter(payment=payment, status='pending')
                                .select_related('plan', 'router')
                                .first()
                            )
                            if hotspot_session:
                                hotspot_session.mark_paid(payment.mpesa_receipt or '')
                                logger.info(
                                    f"HotspotSession {hotspot_session.session_id} marked paid "
                                    f"via Daraja callback (receipt={payment.mpesa_receipt})"
                                )
                        except Exception as hs_err:
                            logger.warning(f"Could not update hotspot session from Daraja callback: {hs_err}")
                        
                        # ============================================================
                        # FIX: Update MpesaTransaction with idempotency check
                        # Only call mark_completed if not already COMPLETED
                        # ============================================================
                        try:
                            # Refresh from DB to get latest status
                            mpesa_transaction.refresh_from_db()
                            if mpesa_transaction.status != 'COMPLETED':
                                mpesa_transaction.mark_completed(
                                    transaction_id=mpesa_receipt,
                                    callback_data=callback_data
                                )
                                logger.info(f"MpesaTransaction {checkout_request_id} marked COMPLETED")
                            else:
                                logger.info(
                                    f"MpesaTransaction {checkout_request_id} already completed "
                                    f"(C2B path), skipping mark_completed"
                                )
                        except Exception as e:
                            # Already completed by a parallel callback - ignore
                            logger.warning(f"MpesaTransaction already completed: {e}")
                        
                        # SMS BLOCK REMOVED - Dead code cleanup. Real SMS already sent via:
                        # - SMSNotifier.pppoe_renewal() for PPPoE payments (above)
                        # - hotspot_session.mark_paid() for hotspot payments (above)
                        
                        return Response({
                            'ResultCode': 0,
                            'ResultDesc': 'Success',
                            'payment_id': payment.id,
                            'receipt_number': payment.mpesa_receipt
                        })
                    finally:
                        # ── NEW: Always release the lock ──
                        if lock_key:
                            cache.delete(lock_key)
                else:
                    logger.warning(f"No payment linked to transaction {checkout_request_id}")
                    
            except MpesaTransaction.DoesNotExist:
                logger.warning(f"Transaction not found for checkout_request_id: {checkout_request_id}")
        
        # Return success to M-Pesa even if we couldn't process (they'll retry)
        return Response({
            'ResultCode': 0,
            'ResultDesc': 'Accepted'
        })

    # === Bank Transfer ===
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
        
        from ..models.payment_models import InvoiceItemPayment, Payment
        try:
            payment_method = InvoiceItemPayment.objects.get(
                method_type='BANK_TRANSFER',
                schema_name=connection.schema_name,
                is_active=True
            )
        except InvoiceItemPayment.DoesNotExist:
            return Response({'status': 'error', 'message': 'Bank transfer payment method not configured'}, status=status.HTTP_400_BAD_REQUEST)
        
        # FIX 4: Create payment with service_type='PPPOE'
        payment = Payment.objects.create(
            schema_name=connection.schema_name,
            customer=customer,
            invoice=invoice,
            amount=amount,
            payment_method=payment_method,
            status='PENDING',
            payment_reference=transaction_reference,
            bank_name=bank_name,
            account_number=account_number,
            payer_name=customer.full_name,
            created_by=request.user,
            service_type='PPPOE',   # Permanent classification for analytics
        )
        
        return Response({
            'status': 'success',
            'message': 'Bank transfer payment recorded',
            'payment_id': payment.id,
            'payment_number': payment.payment_number
        })

    # === FIXED: Dashboard Stats with Proper Aliasing ===
    @action(detail=False, methods=['get'])
    def dashboard_stats(self, request):
        """Get payment dashboard statistics with flattened keys for the frontend"""
        from django.db.models import F
        queryset = self.get_queryset()
        
        today = timezone.now().date()
        yesterday = today - timezone.timedelta(days=1)
        thirty_days_ago = today - timezone.timedelta(days=30)
        
        # Today's payments
        today_payments = queryset.filter(payment_date__date=today)
        today_count = today_payments.count()
        today_amount = float(today_payments.aggregate(Sum('amount'))['amount__sum'] or 0)
        
        # Yesterday's payments
        yesterday_payments = queryset.filter(payment_date__date=yesterday)
        yesterday_count = yesterday_payments.count()
        yesterday_amount = float(yesterday_payments.aggregate(Sum('amount'))['amount__sum'] or 0)
        
        # This month's payments
        month_start = today.replace(day=1)
        month_payments = queryset.filter(payment_date__date__gte=month_start)
        month_count = month_payments.count()
        month_amount = float(month_payments.aggregate(Sum('amount'))['amount__sum'] or 0)
        
        # Status distribution
        # Note: Since queryset only has COMPLETED, we need to query all payments for stats
        all_payments = Payment.objects.filter(schema_name=connection.schema_name)
        status_counts = {
            'PENDING': all_payments.filter(status='PENDING').count(),
            'COMPLETED': all_payments.filter(status='COMPLETED').count(),
            'FAILED': all_payments.filter(status='FAILED').count(),
            'REFUNDED': all_payments.filter(status='REFUNDED').count(),
        }
        
        # 1. Aliased Method Distribution - FIXED: Let the DB do the sum first
        method_distribution = queryset.values(
            name=F('payment_method__name')
        ).annotate(
            count=Count('id'),
            total_sum=Sum('amount')  # Let the DB do the sum first
        ).order_by('-total_sum')
        
        # Format the method distribution with proper float conversion
        formatted_distribution = [
            {
                'name': item['name'],
                'count': item['count'],
                'total': float(item['total_sum'] or 0)
            } for item in method_distribution
        ]
        
        # 2. Aliased Top Payers - FIXED: Let the DB do the sum first
        top_payers_raw = queryset.filter(
            payment_date__date__gte=thirty_days_ago,
            status='COMPLETED'
        ).annotate(
            customer_code=F('customer__customer_code'),
            first_name=F('customer__user__first_name'),
            last_name=F('customer__user__last_name')
        ).values(
            'customer_code', 'first_name', 'last_name'
        ).annotate(
            total_paid_sum=Sum('amount'),
            payment_count=Count('id')
        ).order_by('-total_paid_sum')[:10]
        
        # Format the top payers with proper float conversion
        formatted_top_payers = [
            {
                'customer_code': item['customer_code'],
                'first_name': item['first_name'],
                'last_name': item['last_name'],
                'total_paid': float(item['total_paid_sum'] or 0),
                'payment_count': item['payment_count']
            } for item in top_payers_raw
        ]
        
        # M-Pesa stats
        mpesa_stats = self.get_mpesa_stats(queryset)

        # Flat totals for frontend stat cards
        completed_qs = all_payments.filter(status='COMPLETED')
        pending_qs = all_payments.filter(status='PENDING')
        total_collected = float(completed_qs.aggregate(s=Sum('amount'))['s'] or 0)
        total_pending = float(pending_qs.aggregate(s=Sum('amount'))['s'] or 0)
        
        stats = {
            'today': {'count': today_count, 'amount': today_amount},
            'yesterday': {'count': yesterday_count, 'amount': yesterday_amount},
            'this_month': {'count': month_count, 'amount': month_amount},
            'status_distribution': status_counts,
            'method_distribution': formatted_distribution,
            'top_payers': formatted_top_payers,
            'mpesa_stats': mpesa_stats,
            # Flat keys for frontend cards
            'total_collected': total_collected,
            'total_pending': total_pending,
            'completed_count': status_counts.get('COMPLETED', 0),
            'pending_count': status_counts.get('PENDING', 0),
            'failed_count': status_counts.get('FAILED', 0),
        }
        
        return Response(stats)

    # === FIXED: get_mpesa_stats method ===
    def get_mpesa_stats(self, queryset):
        """Get M-Pesa specific statistics using active schema context"""
        from ..models.payment_models import MpesaTransaction
        
        # Get the schema name from the current DB context
        schema_name = connection.schema_name
        
        mpesa_payments = queryset.filter(payment_method__method_type__startswith='MPESA_')
        
        mpesa_txn_stats = MpesaTransaction.objects.filter(
            schema_name=schema_name
        ).aggregate(
            total_transactions=Count('id'),
            successful=Count('id', filter=Q(status='COMPLETED')),
            failed=Count('id', filter=Q(status='FAILED')),
            total_amount_sum=Sum('amount', filter=Q(status='COMPLETED'))
        )
        
        # Format with proper float conversion
        formatted_txn_stats = {
            'total_transactions': mpesa_txn_stats['total_transactions'] or 0,
            'successful': mpesa_txn_stats['successful'] or 0,
            'failed': mpesa_txn_stats['failed'] or 0,
            'total_amount': float(mpesa_txn_stats['total_amount_sum'] or 0)
        }
        
        return {
            'total_mpesa_payments': mpesa_payments.count(),
            'mpesa_amount': float(mpesa_payments.aggregate(Sum('amount'))['amount__sum'] or 0),
            'transaction_stats': formatted_txn_stats
        }


# ==========================
# Receipt Views (Updated)
# ==========================

class ReceiptViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing receipts
    """
    permission_classes = [IsAuthenticated, IsCompanyStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/receipts"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['status', 'customer']
    search_fields = ['receipt_number', 'customer__customer_code']

    def get_queryset(self):
        from ..models.payment_models import Receipt
        
        user = self.request.user
        
        if user.is_superuser:
            # For superusers, optionally filter by schema_name
            schema_name = self.request.query_params.get('schema_name')
            if schema_name:
                return Receipt.objects.filter(schema_name=schema_name)
            return Receipt.objects.all()
        
        queryset = Receipt.objects.filter(schema_name=connection.schema_name)
        
        if hasattr(user, 'customer_profile'):
            return queryset.filter(customer=user.customer_profile)
        
        return queryset

    def get_serializer_class(self):
        return ReceiptSerializer

    def perform_create(self, serializer):
        user = self.request.user
        
        serializer.save(
            created_by=user,
            schema_name=connection.schema_name
        )

    @action(detail=True, methods=['post'])
    def issue(self, request, pk=None):
        receipt = self.get_object()
        if receipt.issue_receipt(request.user):
            return Response({'status': 'success', 'message': 'Receipt issued'})
        return Response({'error': 'Cannot issue receipt'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def download_pdf(self, request, pk=None):
        receipt = self.get_object()
        serializer = self.get_serializer(receipt)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def share(self, request, pk=None):
        receipt = self.get_object()
        share_method = request.query_params.get('method', 'email')
        
        if share_method == 'sms':
            try:
                from apps.core.models import Company
                # Find the company matching the current active tenant
                current_company = Company.objects.filter(tenant__schema_name=connection.schema_name).first()
                
                if current_company and receipt.customer and receipt.customer.user and receipt.customer.user.phone_number:
                    sms_service = SMSService(company=current_company)
                    message = f"Receipt {receipt.receipt_number} for KES {receipt.amount} issued. Thank you!"
                    result = sms_service.send_single_sms(
                        receipt.customer.user.phone_number, message
                    )
                    if result.get('success'):
                        return Response({'status': 'success', 'message': 'Receipt sent via SMS'})
                    return Response({'error': 'Failed to send SMS'}, status=status.HTTP_400_BAD_REQUEST)
                else:
                    logger.warning(f"Cannot send SMS: missing company or customer phone for schema {connection.schema_name}")
                    return Response({'error': 'Unable to send SMS - missing recipient or company configuration'}, 
                                   status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                logger.error(f"SMS sending error: {str(e)}")
                return Response(
                    {'error': str(e)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
        
        return Response({'error': 'Invalid share method'}, status=status.HTTP_400_BAD_REQUEST)