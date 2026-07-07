from django.conf import settings
from django.db import transaction, connection
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django_tenants.utils import schema_context, get_public_schema_name

from apps.core.permissions import HasRoleAccessPolicy
from apps.core.models import Tenant, Company
from apps.billing.models.payment_models import (
    TenantTumaConfig,
    Payment,
    InvoiceItemPayment,
    StkCancellationTracker
)
from apps.customers.models import Customer
from apps.billing.serializers.tuma_serializers import TenantTumaConfigSerializer
from apps.billing.services.tuma_service import TumaClient, ensure_child_business

import logging
logger = logging.getLogger(__name__)


class TumaBanksView(APIView):
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payment-methods"

    def get(self, request):
        try:
            client = TumaClient()
            token = client.get_master_token()
            return Response(client.list_banks(token))
        except Exception as e:
            return Response({"success": False, "error": str(e)}, status=500)


class TumaCreateChildBusinessView(APIView):
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payment-methods"

    def post(self, request):
        schema = connection.schema_name

        required = ["name", "email", "mobile", "bank_id", "account_number"]
        missing = [f for f in required if f not in request.data]
        if missing:
            return Response({"success": False, "error": f"Missing required fields: {', '.join(missing)}"}, status=400)

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
            master_token = client.get_master_token()
            res = client.create_business(master_token, payload)
            if not res.get("success"):
                return Response(res, status=400)

            data = res["data"]
            cfg, created = TenantTumaConfig.objects.get_or_create(schema_name=schema)

            cfg.tuma_business_id = data.get("id", "")
            cfg.tuma_business_email = data.get("email", "")
            cfg.tuma_business_api_key = data.get("api_key", "")
            cfg.bank_id = data.get("bank_id", "")
            cfg.bank_name = data.get("bank_name", "")
            cfg.bank_account_number = data.get("account_number", "")
            cfg.is_active = True
            cfg.save()

            return Response({"success": True, "business_id": cfg.tuma_business_id, "created": created})
        except Exception as e:
            return Response({"success": False, "error": str(e)}, status=500)


class TumaTenantModeView(APIView):
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payment-methods"

    def _resolve_tenant_identity(self, schema_name):
        """
        Safely resolve tenant and company details without crashing if profile is missing.
        """
        with schema_context(get_public_schema_name()):
            tenant = Tenant.objects.get(schema_name=schema_name)
            try:
                # FIX: Safely try to get the company, catch the error if it doesn't exist
                company = tenant.company 
                name = company.name or tenant.schema_name
                email = company.email or f"{tenant.schema_name}@netily.co.ke"
                mobile = company.phone_number or "254700000000"
            except (Company.DoesNotExist, AttributeError):
                # FALLBACK: If no company profile, use the schema name
                name = tenant.schema_name
                email = f"{tenant.schema_name}@netily.co.ke"
                mobile = "254700000000"

        return tenant, name, email, mobile

    def get(self, request):
        schema = connection.schema_name
        try:
            cfg = TenantTumaConfig.objects.get(schema_name=schema)
            return Response(TenantTumaConfigSerializer(cfg).data)
        except TenantTumaConfig.DoesNotExist:
            return Response({"success": False, "error": "Tuma not configured for this tenant"}, status=404)

    @transaction.atomic
    def put(self, request):
        schema = connection.schema_name
        tenant, name, email, mobile = self._resolve_tenant_identity(schema)

        # 1. Fetch or create using ONLY schema_name as the canonical key
        cfg, _ = TenantTumaConfig.objects.get_or_create(schema_name=schema)

        new_mode = request.data.get("active_mode") or cfg.active_mode
        new_ref_id = request.data.get("collection_reference_id") or cfg.collection_reference_id
        new_acc_num = request.data.get("collection_account_number") or cfg.collection_account_number

        if not new_ref_id:
            return Response({"success": False, "error": "collection_reference_id is required"}, status=400)
        if not new_acc_num:
            return Response({"success": False, "error": "collection_account_number is required"}, status=400)

        # 2. Validate against Tuma API
        client = TumaClient()
        try:
            master_token = client.get_master_token()
        except Exception as e:
            return Response({"success": False, "error": f"Tuma Master Auth failed: {str(e)}"}, status=500)

        banks_res = client.list_banks(master_token)
        banks_list = banks_res.get("data", [])
        
        ref = next((b for b in banks_list if str(b.get("id")) == str(new_ref_id)), None)
        if not ref:
            return Response({"success": False, "error": f"Invalid reference_id: {new_ref_id}"}, status=400)

        # 3. Update configuration object locally
        cfg.active_mode = new_mode
        cfg.collection_reference_id = new_ref_id
        cfg.collection_account_number = new_acc_num
        cfg.collection_reference_code = ref.get("code", "")
        cfg.collection_reference_name = ref.get("name", "")

        # Auto-determine Active Mode if not strictly provided
        code = cfg.collection_reference_code.upper()
        if not request.data.get("active_mode"):
            cfg.active_mode = "TILL" if code in ["TILL", "PAYBILL", "BUYGOODS"] else "BANK"

        # 4. Sync updates to Tuma Servers with STRICT failure handling
        payload = {
            "name": name,
            "email": email,
            "mobile": mobile,
            "bank_id": cfg.collection_reference_id,
            "account_number": cfg.collection_account_number,
            "logo": getattr(settings, "TUMA_DEFAULT_LOGO_URL", "https://placehold.co/400x400.png"),
            "description": f"{tenant.schema_name} payment profile - {cfg.collection_reference_name}",
        }

        if not cfg.tuma_business_id:
            # CREATE NEW BUSINESS
            try:
                res = client.create_business(master_token, payload)
                if not res.get("success"):
                    return Response({"success": False, "error": res.get("message", "Failed to create business")}, status=400)
                
                d = res.get("data", {})
                cfg.tuma_business_id = d.get("id", "")
                cfg.tuma_business_api_key = d.get("api_key", "")
                cfg.tuma_business_email = email 
                
            except Exception as e:
                # STRICT FAILURE: Do not save local config if remote creation fails
                return Response({"success": False, "error": f"API Error creating business: {str(e)}"}, status=500)
        else:
            # UPDATE EXISTING BUSINESS
            try:
                res = client.update_business(master_token, cfg.tuma_business_id, payload)
                if not res.get("success"):
                    return Response({"success": False, "error": res.get("message", "Failed to update business")}, status=400)
                
                cfg.tuma_business_email = email
            except Exception as e:
                logger.error(f"Failed to update remote business: {str(e)}")
                # STRICT FAILURE: Do not save local config if remote update fails
                return Response({"success": False, "error": f"Failed to sync update to Tuma servers. Gateway not saved. Error: {str(e)}"}, status=500)

        # 5. Only save to DB if Tuma succeeded
        cfg.is_active = True
        cfg.save()

        return Response({
            "success": True,
            "business_id": cfg.tuma_business_id,
            "active_mode": cfg.active_mode,
            "reference_name": cfg.collection_reference_name,
            "account_number": cfg.collection_account_number
        })


class TumaInitiatePaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def _check_stk_cancellation_block(self, schema_name, phone_number):
        tracker = StkCancellationTracker.get_or_create_tracker(schema_name, phone_number)
        if tracker.is_currently_blocked() or tracker.consecutive_1032_count >= 3:
            return Response({
                "success": False,
                "error": "STK requests blocked due to multiple cancellations.",
                "detail": "Please contact support to unblock your number."
            }, status=429)
        return None

    @transaction.atomic
    def post(self, request):
        schema = connection.schema_name

        required = ["customer_id", "payment_method_id", "amount", "phone"]
        missing = [f for f in required if f not in request.data]
        if missing:
            return Response({"success": False, "error": f"Missing required fields: {', '.join(missing)}"}, status=400)

        try:
            amount = float(request.data["amount"])
            if amount <= 0:
                raise ValueError()
        except Exception:
            return Response({"success": False, "error": "Invalid amount"}, status=400)

        phone = request.data["phone"]
        blocked = self._check_stk_cancellation_block(schema, phone)
        if blocked:
            return blocked

        try:
            cfg = TenantTumaConfig.objects.get(schema_name=schema, is_active=True)
        except TenantTumaConfig.DoesNotExist:
            return Response({"success": False, "error": "Tuma not configured for this tenant"}, status=400)

        customer = get_object_or_404(Customer, id=request.data["customer_id"], schema_name=schema)
        method = get_object_or_404(InvoiceItemPayment, id=request.data["payment_method_id"], schema_name=schema, is_active=True)

        if not method.is_amount_valid(amount):
            return Response({"success": False, "error": f"Amount must be between {method.minimum_amount} and {method.maximum_amount}"}, status=400)

        transaction_fee = method.calculate_fee(amount)
        net_amount = amount - transaction_fee

        try:
            client = TumaClient()
            if not cfg.tuma_business_email or not cfg.tuma_business_api_key:
                return Response({"success": False, "error": "Tuma business not fully configured."}, status=400)

            token = client.get_token(cfg.tuma_business_email, cfg.tuma_business_api_key)

            callback_url = getattr(settings, "TUMA_CALLBACK_URL", None)
            if not callback_url:
                sub_domain = schema.replace("tenant_", "")
                callback_url = f"https://{sub_domain}.netily.co.ke/api/v1/webhooks/tuma/callback/"

            # Create a simple description that will be cleaned in the service
            description = f"PAY-{customer.customer_code}"

            tuma_res = client.stk_push(
                token=token,
                amount=amount,
                phone=phone,
                callback_url=callback_url,
                description=description,
            )

            if not tuma_res.get("success"):
                return Response(tuma_res, status=400)

            d = tuma_res["data"]
            
            # Generate internal payment reference
            import time
            payment_reference = f"PAY-{customer.customer_code}-{int(time.time())}".replace(" ", "-")
            
            payment = Payment.objects.create(
                customer=customer,
                payment_method=method,
                amount=amount,
                transaction_fee=transaction_fee,
                net_amount=net_amount,
                currency=request.data.get("currency", "KES"),
                status="PROCESSING",
                payment_reference=payment_reference,
                tuma_status="pending",
                tuma_merchant_request_id=d.get("merchant_request_id", ""),
                tuma_checkout_request_id=d.get("checkout_request_id", ""),
                payer_name=customer.full_name,
                payer_phone=phone,
                payer_email=getattr(customer.user, "email", ""),
                schema_name=schema,
            )

            invoice_id = request.data.get("invoice_id")
            if invoice_id:
                from apps.billing.models.billing_models import Invoice
                try:
                    invoice = Invoice.objects.get(id=invoice_id, schema_name=schema)
                    payment.invoice = invoice
                    payment.save()
                except Invoice.DoesNotExist:
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
            return Response({"success": False, "error": f"Payment initiation failed: {str(e)}"}, status=500)
