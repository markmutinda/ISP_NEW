import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.services.netily_paybill_service import NetilyPaybillError, query_stk_status, stk_push_own_paybill
from apps.subscriptions.models import CompanySubscription, SubscriptionPayment

from .models import Company, TenantActivationPayment
from .serializers import CompanyRegisterSerializer
from .views import PlatformCompanyRegisterView

logger = logging.getLogger(__name__)
ACTIVATION_AMOUNT = Decimal("500.00")
TERMINAL_FAILURE_CODES = {"1", "1032", "1037", "2001"}


def _operator_allowed(request):
    PlatformCompanyRegisterView._enforce_registration_access(request)


def _normalize_phone(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if digits.startswith("0") and len(digits) == 10:
        digits = f"254{digits[1:]}"
    elif len(digits) == 9:
        digits = f"254{digits}"
    if not (digits.startswith("254") and len(digits) == 12):
        raise serializers.ValidationError({"payment_phone": "Enter a valid Kenyan mobile number."})
    return digits


def _payload(payment):
    data = payment.registration_data or {}
    return {
        "id": str(payment.id),
        "status": payment.status,
        "amount": str(payment.amount),
        "currency": payment.currency,
        "company_name": data.get("company_name", ""),
        "phone_number": payment.phone_number,
        "receipt": payment.mpesa_receipt or None,
        "failure_reason": payment.failure_reason or None,
        "can_set_password": payment.status == "completed",
        "account_created": payment.status == "consumed",
        "created_at": payment.created_at,
        "completed_at": payment.completed_at,
    }


def apply_activation_callback(callback):
    """Apply an unmatched Netily paybill callback to a registration activation."""
    checkout = callback.get("checkout_request_id")
    reference = callback.get("account_reference")
    query = Q()
    if checkout:
        query |= Q(checkout_request_id=checkout)
    if reference and str(reference).startswith("NETILY-ACT-"):
        query |= Q(account_reference=reference)
    if not query.children:
        return False

    payment = TenantActivationPayment.objects.filter(query).first()
    if not payment:
        return False

    if str(callback.get("result_code")) == "0":
        TenantActivationPayment.objects.filter(
            pk=payment.pk, status__in=["pending", "processing"]
        ).update(
            status="completed",
            completed_at=timezone.now(),
            mpesa_receipt=callback.get("mpesa_receipt") or payment.mpesa_receipt,
            failure_reason="",
        )
    else:
        TenantActivationPayment.objects.filter(
            pk=payment.pk, status__in=["pending", "processing"]
        ).update(
            status="failed",
            failure_reason=callback.get("result_desc") or "M-Pesa did not complete the payment.",
        )
    return True


class TenantActivationStartView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        _operator_allowed(request)
        raw = dict(request.data or {})
        payment_phone = _normalize_phone(raw.pop("payment_phone", ""))

        validation_data = {
            **raw,
            "admin_password": f"Tmp!{uuid.uuid4().hex}A1",
        }
        serializer = CompanyRegisterSerializer(data=validation_data)
        serializer.is_valid(raise_exception=True)
        registration_data = dict(serializer.validated_data)
        registration_data.pop("admin_password", None)
        if registration_data.get("attribution_token"):
            registration_data["attribution_token"] = str(registration_data["attribution_token"])

        reference = f"NETILY-ACT-{uuid.uuid4().hex[:10].upper()}"
        with schema_context(get_public_schema_name()):
            existing = TenantActivationPayment.objects.filter(
                created_by=request.user,
                registration_data__company_email=registration_data["company_email"],
                status__in=["pending", "processing", "completed", "provisioning"],
                expires_at__gt=timezone.now(),
            ).first()
            if existing:
                return Response(_payload(existing), status=status.HTTP_200_OK)
            activation = TenantActivationPayment.objects.create(
                status="pending",
                amount=ACTIVATION_AMOUNT,
                currency="KES",
                phone_number=payment_phone,
                registration_data=registration_data,
                account_reference=reference,
                created_by=request.user,
                expires_at=timezone.now() + timedelta(hours=24),
            )

        callback_url = (
            getattr(settings, "NETILY_REGISTRATION_ACTIVATION_CALLBACK", "")
            or getattr(settings, "NETILY_SUBSCRIPTION_PAYBILL_CALLBACK", "")
            or settings.NETILY_PAYBILL_CALLBACK_URL
        )
        try:
            result = stk_push_own_paybill(
                amount=ACTIVATION_AMOUNT,
                phone_number=payment_phone,
                account_reference=reference,
                transaction_desc="Netily activate",
                callback_url=callback_url,
            )
        except NetilyPaybillError as exc:
            with schema_context(get_public_schema_name()):
                TenantActivationPayment.objects.filter(pk=activation.pk).update(
                    status="failed", failure_reason=str(exc)
                )
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        with schema_context(get_public_schema_name()):
            activation.checkout_request_id = result.get("checkout_request_id")
            activation.merchant_request_id = result.get("merchant_request_id", "")
            activation.status = "processing"
            activation.save(update_fields=["checkout_request_id", "merchant_request_id", "status", "updated_at"])
        return Response(_payload(activation), status=status.HTTP_202_ACCEPTED)


class TenantActivationStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, payment_id):
        _operator_allowed(request)
        with schema_context(get_public_schema_name()):
            payment = TenantActivationPayment.objects.filter(pk=payment_id).first()
            if not payment:
                return Response({"detail": "Activation payment not found."}, status=404)
            if payment.expires_at <= timezone.now() and payment.status in ["pending", "processing"]:
                payment.status = "expired"
                payment.failure_reason = "This payment request expired before it was confirmed."
                payment.save(update_fields=["status", "failure_reason", "updated_at"])
            if payment.status in ["pending", "processing"] and payment.checkout_request_id:
                try:
                    result = query_stk_status(checkout_request_id=payment.checkout_request_id)
                    code = str(result.get("ResultCode", "")).strip()
                    if code == "0":
                        payment.status = "completed"
                        payment.completed_at = timezone.now()
                        payment.mpesa_receipt = payment.mpesa_receipt or result.get("MpesaReceiptNumber", "")
                        payment.failure_reason = ""
                        payment.save(update_fields=["status", "completed_at", "mpesa_receipt", "failure_reason", "updated_at"])
                    elif code in TERMINAL_FAILURE_CODES:
                        payment.status = "failed"
                        payment.failure_reason = result.get("ResultDesc") or "M-Pesa did not complete the payment."
                        payment.save(update_fields=["status", "failure_reason", "updated_at"])
                except NetilyPaybillError:
                    pass
            return Response(_payload(payment))


class TenantActivationFinishView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, payment_id):
        _operator_allowed(request)
        password = str((request.data or {}).get("admin_password") or "")
        confirmation = str((request.data or {}).get("admin_password_confirm") or "")
        if password != confirmation:
            return Response({"admin_password_confirm": ["Passwords do not match."]}, status=400)

        password_probe = CompanyRegisterSerializer(data={
            "company_name": "Password validation",
            "company_email": f"probe-{uuid.uuid4().hex}@example.com",
            "admin_first_name": "Password",
            "admin_last_name": "Check",
            "admin_email": f"admin-{uuid.uuid4().hex}@example.com",
            "admin_phone": "+254700000000",
            "admin_password": password,
        })
        password_probe.is_valid(raise_exception=True)

        with schema_context(get_public_schema_name()):
            claimed = TenantActivationPayment.objects.filter(
                pk=payment_id, status="completed", expires_at__gt=timezone.now()
            ).update(status="provisioning")
            if not claimed:
                payment = TenantActivationPayment.objects.filter(pk=payment_id).first()
                if payment and payment.status == "consumed":
                    return Response({"detail": "This activation payment has already created an account."}, status=409)
                return Response({"detail": "Payment must be confirmed before the account is created."}, status=409)
            payment = TenantActivationPayment.objects.get(pk=payment_id)

        registration_data = {**payment.registration_data, "admin_password": password}
        serializer = CompanyRegisterSerializer(data=registration_data)
        serializer.is_valid(raise_exception=True)
        try:
            response = PlatformCompanyRegisterView()._create_company(
                request, serializer.validated_data, logger
            )
            with schema_context(get_public_schema_name()):
                company = Company.objects.get(
                    name__iexact=registration_data["company_name"],
                    email__iexact=registration_data["company_email"],
                )
                subscription = CompanySubscription.objects.get(company=company)
                SubscriptionPayment.objects.create(
                    subscription=subscription,
                    intended_plan=subscription.plan,
                    intended_billing_period=subscription.billing_period,
                    amount=payment.amount,
                    currency=payment.currency,
                    payment_method="mpesa_stk",
                    phone_number=payment.phone_number,
                    mpesa_receipt=payment.mpesa_receipt,
                    payhero_checkout_id=payment.checkout_request_id,
                    payhero_reference=payment.account_reference,
                    status="completed",
                    completed_at=payment.completed_at or timezone.now(),
                    activation_applied_at=timezone.now(),
                    period_start=subscription.current_period_start,
                    period_end=subscription.current_period_end,
                )
                payment.status = "consumed"
                payment.company = company
                payment.consumed_at = timezone.now()
                payment.failure_reason = ""
                payment.save(update_fields=["status", "company", "consumed_at", "failure_reason", "updated_at"])
            return response
        except Exception as exc:
            logger.exception("Paid tenant activation provisioning failed: payment=%s", payment_id)
            with schema_context(get_public_schema_name()):
                TenantActivationPayment.objects.filter(pk=payment_id).update(
                    status="completed",
                    failure_reason="Account setup needs to be retried. Your payment is safe.",
                )
            return Response(
                {"detail": "Account setup did not finish. Your payment is safe; retry account creation."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
