import logging
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from .models import SubscriptionPayment
from .billing_lifecycle import complete_subscription_stk_payment

logger = logging.getLogger(__name__)


def _first_value(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _callback_metadata_items(body):
    metadata = body.get("CallbackMetadata") or body.get("callback_metadata") or {}
    items = metadata.get("Item") or metadata.get("item") or []
    return {
        item.get("Name") or item.get("name"): item.get("Value") or item.get("value")
        for item in items
        if isinstance(item, dict)
    }


def _normalize_stk_callback(data):
    """
    Safaricom posts STK callbacks under Body.stkCallback, while some gateway
    proxies post a flat snake_case payload. Accept both so production callbacks
    do not get acknowledged without being matched to a payment.
    """
    body = (data.get("Body") or {}).get("stkCallback") or data.get("stkCallback") or data
    items = _callback_metadata_items(body)

    return {
        "merchant_request_id": _first_value(
            body.get("MerchantRequestID"),
            body.get("merchant_request_id"),
            body.get("merchantRequestId"),
        ),
        "checkout_request_id": _first_value(
            body.get("CheckoutRequestID"),
            body.get("checkout_request_id"),
            body.get("checkoutRequestId"),
        ),
        "result_code": _first_value(body.get("ResultCode"), body.get("result_code")),
        "result_desc": _first_value(body.get("ResultDesc"), body.get("result_desc"), body.get("message")),
        "mpesa_receipt": _first_value(
            items.get("MpesaReceiptNumber"),
            items.get("ReceiptNumber"),
            body.get("mpesa_receipt_number"),
            body.get("mpesa_receipt"),
            body.get("transaction_id"),
        ),
        "phone_number": str(_first_value(items.get("PhoneNumber"), body.get("phone_number"), body.get("phone"))),
        "amount": _first_value(items.get("Amount"), body.get("amount")),
        "account_reference": _first_value(
            body.get("AccountReference"),
            body.get("account_reference"),
            body.get("reference"),
        ),
    }


def _find_subscription_payment(callback):
    checkout_request_id = callback["checkout_request_id"]
    account_reference = callback["account_reference"]

    query = Q()
    if checkout_request_id:
        query |= Q(payhero_checkout_id=checkout_request_id)
    if account_reference:
        query |= Q(payhero_reference=account_reference)

    if query:
        payment = SubscriptionPayment.objects.filter(query).first()
        if payment:
            return payment

    # Recovery path for malformed/proxy callbacks that lose the checkout ID.
    # Only use it when the amount/phone combination points to one recent record.
    phone_number = "".join(ch for ch in callback["phone_number"] if ch.isdigit())
    amount = callback["amount"]
    if not phone_number or amount in ("", None):
        return None

    try:
        amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    except Exception:
        return None

    since = timezone.now() - timedelta(days=2)
    candidates = SubscriptionPayment.objects.filter(
        status__in=["pending", "processing"],
        amount=amount,
        created_at__gte=since,
    )
    matched = []
    for payment in candidates:
        stored_phone = "".join(ch for ch in str(payment.phone_number or "") if ch.isdigit())
        if stored_phone and phone_number.endswith(stored_phone[-9:]):
            matched.append(payment)
    candidates = matched
    if len(candidates) == 1:
        logger.warning(
            "Subscription STK callback matched by amount/phone recovery: payment=%s checkout=%s",
            candidates[0].id,
            checkout_request_id,
        )
        return candidates[0]
    return None


class SubscriptionPaybillCallbackView(APIView):
    """
    PUBLIC STK callback for Netily's own paybill — used exclusively for
    platform subscription payments. Replaces the old Tuma subscription
    callback entirely.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        callback = _normalize_stk_callback(request.data)
        result_code = callback["result_code"]
        checkout_request_id = callback["checkout_request_id"]
        merchant_request_id = callback["merchant_request_id"]

        payment = _find_subscription_payment(callback)
        if not payment:
            logger.warning(
                "Subscription STK callback: no payment match checkout=%s merchant=%s phone=%s amount=%s",
                checkout_request_id,
                merchant_request_id,
                callback["phone_number"],
                callback["amount"],
            )
            return Response({"ResultCode": 0, "ResultDesc": "Accepted"})

        if str(result_code) != "0":
            if payment.status != "completed":
                payment.mark_failed(callback["result_desc"] or "Payment failed")
            return Response({"ResultCode": 0, "ResultDesc": "Accepted"})

        try:
            complete_subscription_stk_payment(payment, mpesa_receipt=callback["mpesa_receipt"])
        except Exception:
            logger.exception("Failed completing subscription payment %s", payment.id)

        return Response({"ResultCode": 0, "ResultDesc": "Accepted"})
