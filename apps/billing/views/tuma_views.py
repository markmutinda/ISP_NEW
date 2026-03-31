# apps/billing/views/tuma_views.py
from django.conf import settings
from django.db import transaction, connection
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django_tenants.utils import schema_context, get_public_schema_name

from apps.core.models import Tenant
from apps.billing.models.payment_models import TenantTumaConfig, Payment, InvoiceItemPayment
from apps.customers.models import Customer
from apps.billing.serializers.tuma_serializers import TenantTumaConfigSerializer
from apps.billing.services.tuma_service import TumaClient


class TumaBanksView(APIView):
    """
    Get list of available banks from Tuma gateway
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            client = TumaClient()
            token = client.auth_token(settings.TUMA_MASTER_EMAIL, settings.TUMA_MASTER_API_KEY)
            banks = client.list_banks(token)
            return Response(banks)
        except Exception as e:
            return Response(
                {"success": False, "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class TumaCreateChildBusinessView(APIView):
    """
    Create a child business on Tuma for a tenant
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        schema = connection.schema_name
        with schema_context(get_public_schema_name()):
            tenant = Tenant.objects.get(schema_name=schema)

        # Validate required fields
        required_fields = ['name', 'email', 'mobile', 'bank_id', 'account_number']
        missing_fields = [field for field in required_fields if field not in request.data]
        if missing_fields:
            return Response(
                {"success": False, "error": f"Missing required fields: {', '.join(missing_fields)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        payload = {
            "name": request.data["name"],
            "email": request.data["email"],
            "mobile": request.data["mobile"],
            "bank_id": request.data["bank_id"],
            "account_number": request.data["account_number"],
            "logo": request.data.get("logo") or settings.TUMA_DEFAULT_LOGO_URL,
            "description": request.data.get("description", "ISP Tenant"),
        }

        try:
            client = TumaClient()
            master_token = client.auth_token(settings.TUMA_MASTER_EMAIL, settings.TUMA_MASTER_API_KEY)
            res = client.create_business(master_token, payload)

            if not res.get("success"):
                return Response(res, status=status.HTTP_400_BAD_REQUEST)

            data = res["data"]
            cfg, created = TenantTumaConfig.objects.get_or_create(
                schema_name=schema,
                defaults={"tenant": tenant}
            )
            cfg.tuma_business_id = data.get("id", "")
            cfg.tuma_business_email = data.get("email", "")
            cfg.tuma_business_api_key = data.get("api_key", "")
            cfg.bank_id = data.get("bank_id", "")
            cfg.bank_name = data.get("bank_name", "")
            cfg.bank_account_number = data.get("account_number", "")
            cfg.save()

            return Response({
                "success": True,
                "business_id": cfg.tuma_business_id,
                "created": created
            })
        except Exception as e:
            return Response(
                {"success": False, "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class TumaTenantModeView(APIView):
    """
    Get or update tenant's Tuma payment mode (Till/Bank)
    When updating, automatically creates or updates the child business on Tuma.
    """
    permission_classes = [IsAuthenticated]

    def _resolve_tenant_identity(self, schema_name):
        """
        Resolve tenant identity information for Tuma child business creation.
        Uses company information from the tenant's company profile.
        
        Args:
            schema_name: The tenant's schema name
            
        Returns:
            tuple: (tenant, name, email, mobile)
        """
        with schema_context(get_public_schema_name()):
            tenant = Tenant.objects.get(schema_name=schema_name)

        # Get company information from tenant's company
        # This provides clean mapping for Tuma business registration
        company = getattr(tenant, 'company', None)
        
        if company:
            # Use company information for clean mapping
            name = getattr(company, 'name', None) or getattr(tenant, "name", None) or tenant.schema_name
            email = getattr(company, 'email', '')
            mobile = getattr(company, 'phone_number', '')
        else:
            # Fallback to tenant attributes if company not available
            name = getattr(tenant, "name", None) or tenant.schema_name
            # Get admin user (tenant owner) contact info as fallback
            admin_user = getattr(tenant, "owner", None)
            email = getattr(admin_user, "email", "") if admin_user else ""
            mobile = getattr(admin_user, "phone_number", "") if admin_user else ""
        
        # Ensure we have valid values (no empty strings for required fields)
        if not name:
            name = tenant.schema_name.replace('tenant_', '').title()
        
        return tenant, name, email, mobile

    def get(self, request):
        """Get current Tuma configuration for the tenant"""
        schema = connection.schema_name
        try:
            cfg = TenantTumaConfig.objects.get(schema_name=schema)
            serializer = TenantTumaConfigSerializer(cfg)
            return Response(serializer.data)
        except TenantTumaConfig.DoesNotExist:
            return Response(
                {"success": False, "error": "Tuma not configured for this tenant"},
                status=status.HTTP_404_NOT_FOUND
            )

    @transaction.atomic
    def put(self, request):
        """
        Update tenant's Tuma payment mode.
        Automatically creates or updates the child business on Tuma.
        
        Expected input:
        {
            "collection_reference_id": "bank_equity_123",
            "collection_account_number": "0123456789"
        }
        """
        schema = connection.schema_name
        
        # Resolve tenant identity using company information
        tenant, name, email, mobile = self._resolve_tenant_identity(schema)
        
        # Get or create configuration
        cfg, created = TenantTumaConfig.objects.get_or_create(
            schema_name=schema,
            defaults={"tenant": tenant}
        )
        
        # Validate and save the simplified input
        serializer = TenantTumaConfigSerializer(cfg, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        cfg = serializer.save()
        
        # Initialize Tuma client and authenticate as master
        client = TumaClient()
        master_token = client.auth_token(settings.TUMA_MASTER_EMAIL, settings.TUMA_MASTER_API_KEY)
        
        # Resolve selected reference details from Tuma reference list
        banks_map = client.get_banks_map(master_token)
        ref = banks_map.get(cfg.collection_reference_id)
        
        if not ref:
            return Response(
                {"success": False, "error": "Invalid reference_id. Please select a valid bank/till option."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Populate reference details from Tuma
        cfg.collection_reference_code = ref.get("code", "")
        cfg.collection_reference_name = ref.get("name", "")
        
        # Determine active_mode based on reference type
        ref_code = cfg.collection_reference_code.upper()
        if ref_code in ["TILL", "PAYBILL", "BUYGOODS"]:
            cfg.active_mode = "TILL"
        else:
            cfg.active_mode = "BANK"
        
        # Prepare payload for Tuma child business using company information
        payload = {
            "name": name,
            "email": email or f"{tenant.schema_name}@netily.co.ke",
            "mobile": mobile or "254700000000",
            "bank_id": cfg.collection_reference_id,
            "account_number": cfg.collection_account_number,
            "logo": getattr(settings, 'TUMA_DEFAULT_LOGO_URL', ''),
            "description": f"{tenant.schema_name} payment profile - {cfg.collection_reference_name}",
        }
        
        # Create or update child business on Tuma
        if not cfg.tuma_business_id:
            # Create new child business
            res = client.create_business(master_token, payload)
            if not res.get("success"):
                return Response(
                    {"success": False, "error": res.get("message", "Failed to create child business on Tuma")},
                    status=status.HTTP_400_BAD_REQUEST
                )
            data = res["data"]
            cfg.tuma_business_id = data.get("id", "")
            cfg.tuma_business_email = data.get("email", "")
            cfg.tuma_business_api_key = data.get("api_key", "")
        else:
            # Update existing child business
            res = client.update_business(master_token, cfg.tuma_business_id, payload)
            if not res.get("success"):
                return Response(
                    {"success": False, "error": res.get("message", "Failed to update child business on Tuma")},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        # Activate configuration
        cfg.is_active = True
        cfg.save()
        
        # Return success response with details
        return Response({
            "success": True,
            "business_id": cfg.tuma_business_id,
            "business_name": name,
            "business_email": email,
            "reference": {
                "id": cfg.collection_reference_id,
                "code": cfg.collection_reference_code,
                "name": cfg.collection_reference_name,
            },
            "account_number": cfg.collection_account_number,
            "active_mode": cfg.active_mode,
        })


class TumaInitiatePaymentView(APIView):
    """
    Initiate a payment via Tuma gateway
    Required fields: customer_id, payment_method_id, amount, phone
    Optional fields: invoice_id, description
    """
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        schema = connection.schema_name
        
        # Ensure customer and method belong to THIS tenant
        customer_id = request.data.get("customer_id")
        method_id = request.data.get("payment_method_id")
        
        # Validate required fields
        required_fields = ['customer_id', 'payment_method_id', 'amount', 'phone']
        missing_fields = [field for field in required_fields if field not in request.data]
        if missing_fields:
            return Response(
                {"success": False, "error": f"Missing required fields: {', '.join(missing_fields)}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate amount
        try:
            amount = float(request.data["amount"])
            if amount <= 0:
                raise ValueError("Amount must be positive")
        except (TypeError, ValueError):
            return Response(
                {"success": False, "error": "Invalid amount"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get Tuma configuration
        try:
            cfg = TenantTumaConfig.objects.get(schema_name=schema, is_active=True)
        except TenantTumaConfig.DoesNotExist:
            return Response(
                {"success": False, "error": "Tuma not configured for this tenant"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not cfg.active_mode:
            return Response(
                {"success": False, "error": "No active payment mode set (Till/Bank)"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate customer belongs to THIS tenant using schema_name
        customer = get_object_or_404(Customer, id=customer_id, schema_name=schema)
        
        # Validate payment method belongs to THIS tenant using schema_name
        method = get_object_or_404(InvoiceItemPayment, id=method_id, schema_name=schema, is_active=True)
        
        # Validate payment method amount limits
        if not method.is_amount_valid(amount):
            return Response(
                {"success": False, "error": f"Amount must be between {method.minimum_amount} and {method.maximum_amount}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Calculate fees
        transaction_fee = method.calculate_fee(amount)
        net_amount = amount - transaction_fee
        
        try:
            # Initialize Tuma client
            client = TumaClient()
            
            # Authenticate using child business credentials
            if not cfg.tuma_business_email or not cfg.tuma_business_api_key:
                return Response(
                    {"success": False, "error": "Tuma business not fully configured. Please complete child business setup."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            token = client.auth_token(cfg.tuma_business_email, cfg.tuma_business_api_key)
            
            # Prepare callback URL
            callback_url = getattr(settings, 'TUMA_CALLBACK_URL', None)
            if not callback_url:
                # Construct default callback URL based on tenant schema
                sub_domain = schema.replace('tenant_', '')
                callback_url = f"https://{sub_domain}.netily.co.ke/api/v1/webhooks/tuma/callback/"
            
            # Initiate STK push
            tuma_res = client.stk_push(
                token=token,
                amount=amount,
                phone=request.data["phone"],
                callback_url=callback_url,
                description=request.data.get("description", f"Payment via {cfg.active_mode} - {cfg.collection_reference_name}"),
            )
            
            if not tuma_res.get("success"):
                return Response(tuma_res, status=status.HTTP_400_BAD_REQUEST)
            
            data = tuma_res["data"]
            
            # Create Payment record with all required fields using validated objects
            payment = Payment.objects.create(
                customer=customer,
                payment_method=method,
                amount=amount,
                transaction_fee=transaction_fee,
                net_amount=net_amount,
                currency=request.data.get("currency", "KES"),
                status="PROCESSING",
                payment_reference=request.data.get("payment_reference", ""),
                # Tuma-specific fields
                tuma_status="pending",
                tuma_merchant_request_id=data.get("merchant_request_id", ""),
                tuma_checkout_request_id=data.get("checkout_request_id", ""),
                # Payer information
                payer_name=customer.full_name,
                payer_phone=request.data["phone"],
                payer_email=getattr(customer.user, 'email', ''),
                # Schema
                schema_name=schema,
            )
            
            # Link to invoice if provided (with tenant isolation)
            invoice_id = request.data.get("invoice_id")
            if invoice_id:
                from apps.billing.models.billing_models import Invoice
                try:
                    invoice = Invoice.objects.get(id=invoice_id, schema_name=schema)
                    payment.invoice = invoice
                    payment.save()
                except Invoice.DoesNotExist:
                    # Log but don't fail - payment can exist without invoice
                    pass
            
            return Response({
                "success": True,
                "payment_id": payment.id,
                "payment_number": payment.payment_number,
                "merchant_request_id": payment.tuma_merchant_request_id,
                "checkout_request_id": payment.tuma_checkout_request_id,
                "status": payment.status,
            })
            
        except Exception as e:
            return Response(
                {"success": False, "error": f"Payment initiation failed: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )