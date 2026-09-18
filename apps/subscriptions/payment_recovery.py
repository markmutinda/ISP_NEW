import logging

from django.core.cache import cache
from django_tenants.utils import get_public_schema_name, schema_context

from apps.billing.services.netily_paybill_service import (
    NetilyPaybillError,
    query_stk_status,
)

from .billing_lifecycle import complete_subscription_stk_payment
from .models import SubscriptionPayment

logger = logging.getLogger(__name__)


TERMINAL_FAILURE_CODES = {"1", "1032", "1037", "2001"}


def reconcile_subscription_payment_from_gateway(payment, *, throttle_seconds=8):
    """
    Query Daraja for a pending subscription STK and apply the same lifecycle as
    the callback. This is used by both user-facing polling and the Celery sweep.
    """
    if payment.status not in ["pending", "processing"] or not payment.payhero_checkout_id:
        return payment, {"checked": False, "reason": "not_recoverable"}

    cache_key = f"subscription-stk-query:{payment.id}:{payment.payhero_checkout_id}"
    if throttle_seconds:
        try:
            if not cache.add(cache_key, True, throttle_seconds):
                return payment, {"checked": False, "reason": "throttled"}
        except Exception:
            logger.warning("STK query throttle unavailable: payment=%s", payment.id)

    try:
        gateway_status = query_stk_status(checkout_request_id=payment.payhero_checkout_id)
    except NetilyPaybillError as exc:
        logger.info("Subscription STK status query still pending for payment %s: %s", payment.id, exc)
        return payment, {"checked": True, "gateway_pending": True, "message": str(exc)}
    except Exception as exc:
        logger.warning("Unexpected subscription STK status query error for payment %s: %s", payment.id, exc)
        return payment, {"checked": True, "gateway_error": True, "message": str(exc)}

    result_code = str(gateway_status.get("ResultCode", "")).strip()
    result_desc = gateway_status.get("ResultDesc") or gateway_status.get("errorMessage") or ""

    if result_code == "0":
        with schema_context(get_public_schema_name()):
            payment, _invoice = complete_subscription_stk_payment(
                payment,
                mpesa_receipt=payment.mpesa_receipt or gateway_status.get("MpesaReceiptNumber") or "",
            )
            payment.refresh_from_db()
        return payment, {"checked": True, "gateway_paid": True, "gateway_status": gateway_status}

    if result_code in TERMINAL_FAILURE_CODES:
        with schema_context(get_public_schema_name()):
            SubscriptionPayment.objects.filter(pk=payment.pk, status__in=["pending", "processing"]).update(
                status="failed",
                failure_reason=result_desc or "M-Pesa did not complete the payment.",
            )
            payment.refresh_from_db()
        return payment, {"checked": True, "gateway_failed": True, "gateway_status": gateway_status}

    return payment, {"checked": True, "gateway_unknown": True, "gateway_status": gateway_status}
