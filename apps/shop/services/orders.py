import logging
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING

from django.conf import settings
from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Greatest
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from ..models import Order, OrderItem, Product

logger = logging.getLogger(__name__)


def _check_option(options, key, value, label):
    """Validate only when the product actually defines options (frontend sends 'Standard'/'Default' otherwise)."""
    if not options or not value:
        return
    for opt in options:
        if isinstance(opt, dict) and opt.get(key) == value:
            if opt.get("available", True) is False:
                raise ValidationError({"items": [f"{label} '{value}' is currently unavailable."]})
            return
    raise ValidationError({"items": [f"Invalid {label.lower()} '{value}'."]})


def create_order(data):
    lines_in = data["items"]
    slugs = {l["product_id"] for l in lines_in}
    products = {
        p.slug: p
        for p in Product.objects.filter(slug__in=slugs, is_active=True, category__is_active=True)
    }
    missing = sorted(slugs - set(products))
    if missing:
        raise ValidationError({"items": [f"Product not available: {', '.join(missing)}"]})

    lines, subtotal, needed = [], Decimal("0"), defaultdict(int)
    for l in lines_in:
        p = products[l["product_id"]]
        storage = (l.get("selected_storage") or "")[:120]
        color = (l.get("selected_color") or "")[:120]
        _check_option(p.sizes, "size", storage, "Configuration")
        _check_option(p.colors, "name", color, "Colour")
        qty = l["quantity"]
        line_total = p.price * qty
        subtotal += line_total
        if p.track_stock:
            needed[p.pk] += qty
        lines.append((p, qty, storage, color, line_total))

    shipping = Decimal(str(settings.SHOP_SHIPPING_FEE_USD))
    total = subtotal + shipping
    fx = Decimal(str(settings.SHOP_USD_KES_RATE))
    amount_kes = max(int((total * fx).to_integral_value(rounding=ROUND_CEILING)), 1)

    with transaction.atomic():
        # Atomic reservation, ordered by pk to avoid deadlocks. Raising rolls everything back.
        for pid in sorted(needed):
            updated = Product.objects.filter(
                pk=pid, track_stock=True, stock_quantity__gte=needed[pid]
            ).update(stock_quantity=F("stock_quantity") - needed[pid])
            if not updated:
                name = next(p.name for p, *_ in lines if p.pk == pid)
                raise ValidationError({"items": [f"Not enough stock for {name}."]})

        order = Order.objects.create(
            reference=Order.new_reference(),
            payment_method=data["payment_method"],
            first_name=data["first_name"], last_name=data["last_name"],
            company=data.get("company", ""), email=data["email"], phone=data["phone"],
            address=data["address"], apartment=data.get("apartment", ""),
            city=data["city"], state=data.get("state", ""), zip_code=data.get("zip_code", ""),
            country=data.get("country") or "Kenya", notes=data.get("notes", ""),
            subtotal=subtotal, shipping_fee=shipping, total=total,
            fx_rate=fx, amount_kes=amount_kes,
        )
        OrderItem.objects.bulk_create([
            OrderItem(
                order=order, product=p, product_slug=p.slug, name=p.name, image=p.image,
                unit_price=p.price, quantity=qty, selected_storage=storage,
                selected_color=color, line_total=line_total,
            )
            for p, qty, storage, color, line_total in lines
        ])
    return order


def _quantities(order):
    qty = defaultdict(int)
    for it in order.items.all():
        if it.product_id:
            qty[it.product_id] += it.quantity
    return qty


def release_stock(order):
    """Call with the order row locked inside an atomic block; caller saves 'stock_released'."""
    if order.stock_released:
        return
    for pid, q in sorted(_quantities(order).items()):
        Product.objects.filter(pk=pid, track_stock=True).update(stock_quantity=F("stock_quantity") + q)
    order.stock_released = True


def reserve_stock_best_effort(order):
    """Late payment on an already-expired order: take stock back without ever going negative."""
    for pid, q in sorted(_quantities(order).items()):
        Product.objects.filter(pk=pid, track_stock=True).update(
            stock_quantity=Greatest(F("stock_quantity") - q, Value(0))
        )
    order.stock_released = False


def expire_order(pk):
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=pk).first()
        if not order or order.status != Order.PENDING_PAYMENT or order.payment_status == Order.PAY_PAID:
            return False
        release_stock(order)
        order.status = Order.EXPIRED
        if order.payment_status == Order.PAY_PENDING:
            order.payment_status = Order.PAY_FAILED
            order.payment_failure_reason = "Order expired before payment was completed."
        order.updated_at = timezone.now()
        order.save(update_fields=["status", "payment_status", "payment_failure_reason",
                                  "stock_released", "updated_at"])
        return True