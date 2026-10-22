import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def reconcile_pending_mpesa_orders():
    """Ask Daraja about STK pushes whose callback never arrived."""
    from .models import Order
    from .services import mpesa
    from .services.payments import apply_payment_result

    now = timezone.now()
    ids = list(
        Order.objects.filter(
            status=Order.PENDING_PAYMENT, payment_method=Order.METHOD_MPESA,
            payment_status=Order.PAY_PENDING, checkout_request_id__isnull=False,
            last_stk_at__lt=now - timedelta(minutes=1), last_stk_at__gt=now - timedelta(minutes=30),
        ).values_list("checkout_request_id", flat=True)[:30]
    )
    for cid in ids:
        try:
            result = mpesa.stk_query(cid)
        except mpesa.MpesaError:
            continue
        if result is None:
            continue
        code, desc = result
        apply_payment_result(cid, success=(code == 0), desc=desc)


@shared_task(ignore_result=True)
def expire_stale_orders():
    from .models import Order
    from .services.orders import expire_order

    cutoff = timezone.now() - timedelta(minutes=settings.SHOP_ORDER_EXPIRY_MINUTES)
    ids = list(
        Order.objects.filter(status=Order.PENDING_PAYMENT, created_at__lt=cutoff)
        .exclude(payment_status=Order.PAY_PAID).values_list("pk", flat=True)[:200]
    )
    for pk in ids:
        expire_order(pk)


@shared_task(ignore_result=True)
def send_order_confirmation(order_pk):
    from apps.core.email_delivery import send_transactional_email
    from .models import Order

    order = Order.objects.prefetch_related("items").filter(pk=order_pk).first()
    if not order or not order.email:
        return
    lines = "\n".join(f"- {i.name} x{i.quantity} ({i.selected_storage or '-'})" for i in order.items.all())
    track = f"{settings.FRONTEND_URL.rstrip('/')}/shop/track?ref={order.reference}&token={order.tracking_token}"
    plain = (
        f"Hi {order.first_name},\n\nThanks for your order {order.reference}. Payment received"
        f"{' (M-Pesa ' + order.mpesa_receipt + ')' if order.mpesa_receipt else ''}.\n\n"
        f"{lines}\n\nTotal: ${order.total} USD (KES {order.amount_kes})\n\n"
        f"Track your order: {track}\n\n"
        f"We'll share delivery updates shortly.\nNetily Shop"
    )
    html = "<p>" + plain.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    try:
        send_transactional_email(
            subject=f"Order {order.reference} confirmed", recipient=order.email,
            plain_message=plain, html_message=html, from_email=settings.DEFAULT_FROM_EMAIL,
        )
    except Exception:
        logger.exception("Order confirmation email failed for %s", order.reference)