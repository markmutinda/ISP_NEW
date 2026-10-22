import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from ..models import Order
from . import mpesa
from .orders import release_stock, reserve_stock_best_effort  # noqa: F401

logger = logging.getLogger(__name__)

MAX_STK_PER_PHONE = 3  # per 10 minutes


def initiate_mpesa_payment(order, phone):
    """Send an STK push. Returns (ok, message). Never raises."""
    key = f"shop:stk:{phone}"
    cache.add(key, 0, timeout=600)
    try:
        attempts = cache.incr(key)
    except ValueError:
        attempts = 1
    if attempts > MAX_STK_PER_PHONE:
        return False, "Too many payment requests for this number. Please wait a few minutes."

    now = timezone.now()
    try:
        res = mpesa.stk_push(
            phone=phone, amount=order.amount_kes, reference=order.reference,
            description="Netily Shop", callback_url=mpesa.callback_url(),
        )
    except mpesa.MpesaError as exc:
        Order.objects.filter(pk=order.pk).update(
            payment_status=Order.PAY_FAILED, payment_failure_reason=str(exc)[:255],
            mpesa_phone=phone, updated_at=now,
        )
        return False, str(exc)

    Order.objects.filter(pk=order.pk).update(
        checkout_request_id=res["checkout_request_id"],
        merchant_request_id=res["merchant_request_id"],
        mpesa_phone=phone, payment_status=Order.PAY_PENDING, payment_failure_reason="",
        stk_attempts=F("stk_attempts") + 1, last_stk_at=now, updated_at=now,
    )
    return True, res["customer_message"]


def mark_order_paid(order, receipt=""):
    """Order must be locked (select_for_update) inside an atomic block."""
    order.payment_status = Order.PAY_PAID
    order.payment_failure_reason = ""
    order.paid_at = order.paid_at or timezone.now()
    if receipt:
        order.mpesa_receipt = receipt[:30]
    if order.status in (Order.PENDING_PAYMENT, Order.EXPIRED):
        if order.stock_released:
            reserve_stock_best_effort(order)
        order.status = Order.PAID
    else:
        logger.error("Payment received for order %s in status %s - review/refund manually",
                     order.reference, order.status)
    order.updated_at = timezone.now()
    order.save(update_fields=["payment_status", "payment_failure_reason", "paid_at", "mpesa_receipt",
                              "status", "stock_released", "updated_at"])


def _queue_confirmation(order_pk):
    try:
        from ..tasks import send_order_confirmation
        send_order_confirmation.apply_async(args=[order_pk], retry=False)
    except Exception:
        logger.exception("Could not queue order confirmation for order pk=%s", order_pk)


@transaction.atomic
def apply_payment_result(checkout_request_id, *, success, desc="", receipt="", amount=None):
    """Idempotent. Used by the Safaricom callback and the reconcile task."""
    order = Order.objects.select_for_update().filter(checkout_request_id=checkout_request_id).first()
    if not order:
        return None

    if order.payment_status == Order.PAY_PAID:
        if success and receipt and not order.mpesa_receipt:
            order.mpesa_receipt = receipt[:30]
            order.save(update_fields=["mpesa_receipt", "updated_at"])
        return order

    if success:
        if amount is not None and int(float(amount)) < order.amount_kes:
            logger.error("Shop payment amount mismatch order=%s expected=%s got=%s",
                         order.reference, order.amount_kes, amount)
            order.payment_failure_reason = "Amount mismatch - flagged for review."
            order.save(update_fields=["payment_failure_reason", "updated_at"])
            return order
        mark_order_paid(order, receipt)
        pk = order.pk
        transaction.on_commit(lambda: _queue_confirmation(pk))
    else:
        order.payment_status = Order.PAY_FAILED
        order.payment_failure_reason = (desc or "Payment was not completed.")[:255]
        order.save(update_fields=["payment_status", "payment_failure_reason", "updated_at"])
    return order