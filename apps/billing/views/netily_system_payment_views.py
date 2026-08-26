import base64
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


SIM_CACHE_PREFIX = "netily_system_payment_sim"
SIM_TTL_SECONDS = 60 * 60


def _setting(name, default=""):
    return str(getattr(settings, name, default) or "").strip()


def _simulation_enabled():
    return _setting("NETILY_SYSTEM_PAYMENT_SIMULATOR_ENABLED", "False").lower() in {"1", "true", "yes", "on"}


def _require_simulator_token(request):
    expected = _setting("NETILY_SYSTEM_PAYMENT_SIMULATOR_TOKEN")
    if not expected:
        return False
    supplied = (
        request.headers.get("X-Netily-System-Payment-Token")
        or getattr(request, "query_params", {}).get("test_key")
        or request.data.get("test_key")
        or ""
    )
    return str(supplied).strip() == expected


def _normalize_ke_phone(value):
    phone = "".join(ch for ch in str(value or "") if ch.isdigit())
    if phone.startswith("0") and len(phone) == 10:
        phone = f"254{phone[1:]}"
    elif phone.startswith("7") and len(phone) == 9:
        phone = f"254{phone}"
    if not (phone.startswith("254") and len(phone) == 12 and phone[3] in {"1", "7"}):
        raise ValueError("Enter a valid Safaricom phone number, for example 2547XXXXXXXX.")
    return phone


def _money(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal("1"))
    except (InvalidOperation, TypeError):
        raise ValueError("Enter a valid amount.")
    if amount < Decimal("1"):
        raise ValueError("Amount must be at least KES 1.")
    if amount > Decimal("150000"):
        raise ValueError("Amount is too high for this simulator.")
    return amount


def _cache_key(checkout_request_id):
    return f"{SIM_CACHE_PREFIX}:{checkout_request_id}"


def _callback_url(request):
    configured = _setting("NETILY_SYSTEM_PAYMENT_CALLBACK_URL")
    if configured:
        return configured
    base = _setting("BASE_URL")
    if not base:
        base = request.build_absolute_uri("/").rstrip("/")
    return f"{base.rstrip('/')}/api/v1/billing/netily-system-payment/callback/"


class NetilySystemPaymentInitiateView(APIView):
    """
    Live Daraja STK simulator for the Netily master paybill.

    This intentionally stores status in cache only. It does not create tenant
    payments, subscription payments, invoices, settlements, or payouts.
    """

    authentication_classes = []
    permission_classes = []

    def post(self, request):
        if not _simulation_enabled():
            return Response(
                {"success": False, "message": "Netily system payment simulator is disabled."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not _require_simulator_token(request):
            return Response(
                {"success": False, "message": "Enter the simulator test key to continue."},
                status=status.HTTP_403_FORBIDDEN,
            )

        consumer_key = _setting("NETILY_SYSTEM_MPESA_CONSUMER_KEY")
        consumer_secret = _setting("NETILY_SYSTEM_MPESA_CONSUMER_SECRET")
        shortcode = _setting("NETILY_SYSTEM_MPESA_SHORTCODE")
        passkey = _setting("NETILY_SYSTEM_MPESA_PASSKEY")
        environment = _setting("NETILY_SYSTEM_MPESA_ENVIRONMENT", "production").lower()
        if not all([consumer_key, consumer_secret, shortcode, passkey]):
            return Response(
                {"success": False, "message": "Simulator Daraja credentials are not configured on the server."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            phone = _normalize_ke_phone(request.data.get("phone_number"))
            amount = _money(request.data.get("amount"))
        except ValueError as exc:
            return Response({"success": False, "message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        model = str(request.data.get("model") or "netily_passthrough").lower()
        if model not in {"direct_tenant", "netily_passthrough"}:
            return Response({"success": False, "message": "Choose a valid simulation model."}, status=status.HTTP_400_BAD_REQUEST)

        tenant_code = str(request.data.get("tenant_code") or "DEMO").upper().replace(" ", "")[:8] or "DEMO"
        try:
            fee_rate = Decimal(str(request.data.get("fee_rate") or "2")).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            return Response({"success": False, "message": "Enter a valid processing fee rate."}, status=status.HTTP_400_BAD_REQUEST)
        if fee_rate < Decimal("0") or fee_rate > Decimal("15"):
            return Response({"success": False, "message": "Processing fee must be between 0% and 15%."}, status=status.HTTP_400_BAD_REQUEST)
        account_reference = f"NET-{tenant_code}"[:12]
        description = "NetilySub"[:13]
        api_base = "https://sandbox.safaricom.co.ke" if environment == "sandbox" else "https://api.safaricom.co.ke"

        try:
            token_response = requests.get(
                f"{api_base}/oauth/v1/generate?grant_type=client_credentials",
                auth=(consumer_key, consumer_secret),
                timeout=20,
            )
            token_data = token_response.json()
            access_token = token_data.get("access_token")
            if token_response.status_code != 200 or not access_token:
                logger.warning("Netily simulator Daraja auth failed: %s", token_data)
                return Response(
                    {"success": False, "message": "Could not authenticate with Daraja. Check server credentials."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()
            callback_url = _callback_url(request)
            payload = {
                "BusinessShortCode": shortcode,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": str(int(amount)),
                "PartyA": phone,
                "PartyB": shortcode,
                "PhoneNumber": phone,
                "CallBackURL": callback_url,
                "AccountReference": account_reference,
                "TransactionDesc": description,
            }
            stk_response = requests.post(
                f"{api_base}/mpesa/stkpush/v1/processrequest",
                json=payload,
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                timeout=25,
            )
            stk_data = stk_response.json()
        except requests.RequestException as exc:
            logger.exception("Netily simulator STK network error")
            return Response(
                {"success": False, "message": "Could not reach Daraja. Please try again.", "detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except ValueError:
            logger.exception("Netily simulator received non-JSON Daraja response")
            return Response({"success": False, "message": "Daraja returned an unreadable response."}, status=status.HTTP_502_BAD_GATEWAY)

        if stk_response.status_code == 200 and stk_data.get("ResponseCode") == "0":
            checkout_id = stk_data.get("CheckoutRequestID")
            merchant_id = stk_data.get("MerchantRequestID")
            fee_amount = (amount * fee_rate / Decimal("100")).quantize(Decimal("1"))
            state = {
                "success": True,
                "status": "pending",
                "model": model,
                "phone_number": phone,
                "amount": str(amount),
                "fee_rate": str(fee_rate),
                "fee_amount": str(fee_amount if model == "netily_passthrough" else Decimal("0")),
                "tenant_payout_amount": str(amount - fee_amount if model == "netily_passthrough" else amount),
                "destination_shortcode": shortcode,
                "destination_label": "Netily system Equity paybill",
                "account_reference": account_reference,
                "checkout_request_id": checkout_id,
                "merchant_request_id": merchant_id,
                "customer_message": stk_data.get("CustomerMessage", "Check your phone and enter your M-Pesa PIN."),
                "created_at": timezone.now().isoformat(),
                "callback_url": callback_url,
                "last_result_desc": "",
                "mpesa_receipt": "",
                "safeguard": "Simulation only. No SubscriptionPayment, invoice, tenant wallet, or payout record was created.",
            }
            cache.set(_cache_key(checkout_id), state, SIM_TTL_SECONDS)
            return Response(state)

        message = stk_data.get("ResponseDescription") or stk_data.get("errorMessage") or "STK push was not accepted by Daraja."
        return Response(
            {"success": False, "message": message, "daraja_response": stk_data},
            status=status.HTTP_502_BAD_GATEWAY,
        )


class NetilySystemPaymentStatusView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, checkout_request_id):
        if not _simulation_enabled():
            return Response({"success": False, "message": "Simulator is disabled."}, status=status.HTTP_404_NOT_FOUND)
        if not _require_simulator_token(request):
            return Response({"success": False, "message": "Enter the simulator test key to continue."}, status=status.HTTP_403_FORBIDDEN)
        state = cache.get(_cache_key(checkout_request_id))
        if not state:
            return Response({"success": False, "status": "expired", "message": "Simulation record expired or was not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(state)


class NetilySystemPaymentCallbackView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        body = request.data.get("Body", {}).get("stkCallback", {})
        checkout_id = body.get("CheckoutRequestID")
        result_code = body.get("ResultCode")
        result_desc = body.get("ResultDesc", "")
        if not checkout_id:
            return Response({"ResultCode": 0, "ResultDesc": "Accepted"})

        state = cache.get(_cache_key(checkout_id)) or {
            "success": True,
            "checkout_request_id": checkout_id,
            "status": "orphaned_callback",
            "safeguard": "Callback received but no cache state was found. No system records were changed.",
        }

        if str(result_code) == "0":
            items = body.get("CallbackMetadata", {}).get("Item", [])
            meta = {item.get("Name"): item.get("Value") for item in items}
            state.update(
                {
                    "status": "completed",
                    "mpesa_receipt": meta.get("MpesaReceiptNumber", ""),
                    "amount": str(meta.get("Amount", state.get("amount", ""))),
                    "phone_number": str(meta.get("PhoneNumber", state.get("phone_number", ""))),
                    "completed_at": timezone.now().isoformat(),
                    "last_result_desc": result_desc or "Payment completed.",
                }
            )
        elif str(result_code) == "1032":
            state.update({"status": "cancelled", "last_result_desc": "The STK prompt was cancelled. You can retry immediately."})
        else:
            state.update({"status": "failed", "last_result_desc": result_desc or "Payment failed."})

        cache.set(_cache_key(checkout_id), state, SIM_TTL_SECONDS)
        logger.info("Netily system payment simulator callback: checkout=%s status=%s", checkout_id, state.get("status"))
        return Response({"ResultCode": 0, "ResultDesc": "Accepted"})
