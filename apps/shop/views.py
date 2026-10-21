import hashlib
import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.superadmin.permissions import IsSuperAdmin
from utils.phone import is_valid_phone_number, normalize_phone_number

from .cache import get_catalog_version
from .models import Category, Order, Product
from .serializers import (
    AdminCategorySerializer, AdminOrderDetailSerializer, AdminOrderListSerializer,
    AdminProductSerializer, CategorySerializer, CheckoutSerializer, OrderPublicSerializer,
    ProductSerializer,
)
from .services.orders import create_order, release_stock
from .services.payments import apply_payment_result, initiate_mpesa_payment, mark_order_paid

logger = logging.getLogger(__name__)
CATALOG_TTL = 60


class PublicAPIView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]


def _cached(key, builder, ttl=CATALOG_TTL):
    data = cache.get(key)
    if data is None:
        data = builder()
        cache.set(key, data, ttl)
    return data


def _public_products():
    return Product.objects.filter(is_active=True, category__is_active=True).select_related("category")


# ───────────── Public catalog ─────────────
class CategoryListView(PublicAPIView):
    def get(self, request):
        def build():
            qs = Category.objects.filter(is_active=True)
            return list(CategorySerializer(qs, many=True).data)
        return Response(_cached(f"shop:cats:{get_catalog_version()}", build))


class ProductListView(PublicAPIView):
    ORDERING = {"price": "price", "-price": "-price", "newest": "-created_at", "name": "name"}

    def get(self, request):
        qp = request.query_params
        digest = hashlib.md5(qp.urlencode().encode()).hexdigest()

        def build():
            try:
                page = max(int(qp.get("page", 1)), 1)
                size = min(max(int(qp.get("page_size", 100)), 1), 200)
            except ValueError:
                page, size = 1, 100
            qs = _public_products()
            cat = (qp.get("category") or "").strip()
            if cat:
                qs = qs.filter(Q(category__slug=cat) | Q(category__name__iexact=cat))
            search = (qp.get("search") or "").strip()[:80]
            if search:
                qs = qs.filter(Q(name__icontains=search) | Q(category__name__icontains=search) | Q(slug__icontains=search))
            if qp.get("featured") in ("1", "true"):
                qs = qs.filter(is_featured=True)
            order = self.ORDERING.get(qp.get("ordering"))
            qs = qs.order_by(order, "pk") if order else qs.order_by("sort_order", "name", "pk")
            total = qs.count()
            rows = qs[(page - 1) * size: page * size]
            return {"count": total, "page": page, "page_size": size,
                    "results": list(ProductSerializer(rows, many=True).data)}

        return Response(_cached(f"shop:list:{get_catalog_version()}:{digest}", build))


class ProductDetailView(PublicAPIView):
    def get(self, request, slug):
        def build():
            product = get_object_or_404(_public_products(), slug=slug)
            return dict(ProductSerializer(product).data)
        return Response(_cached(f"shop:prod:{get_catalog_version()}:{slug}", build))


class RelatedProductsView(PublicAPIView):
    def get(self, request, slug):
        try:
            limit = min(max(int(request.query_params.get("limit", 4)), 1), 12)
        except ValueError:
            limit = 4

        def build():
            product = get_object_or_404(_public_products(), slug=slug)
            qs = _public_products().filter(category_id=product.category_id).exclude(pk=product.pk)[:limit]
            return list(ProductSerializer(qs, many=True).data)
        return Response(_cached(f"shop:rel:{get_catalog_version()}:{slug}:{limit}", build))


# ───────────── Public checkout / orders ─────────────
def _get_order(reference, token):
    order = Order.objects.filter(reference=str(reference).upper()).prefetch_related("items").first()
    if not order or not constant_time_compare(order.tracking_token, str(token or "")):
        raise Http404
    return order


class CheckoutView(PublicAPIView):
    throttle_scope = "shop_checkout"

    def post(self, request):
        s = CheckoutSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.validated_data
        order = create_order(data)  # raises ValidationError (400) on stock/option problems
        ok, message = initiate_mpesa_payment(order, data["mpesa_msisdn"])
        order.refresh_from_db(fields=["status", "payment_status"])
        return Response({
            "reference": order.reference,
            "tracking_token": order.tracking_token,
            "status": order.status,
            "payment_status": order.payment_status,
            "total": float(order.total),
            "currency": order.currency,
            "amount_kes": order.amount_kes,
            "payment": {"initiated": ok, "message": message},
        }, status=status.HTTP_201_CREATED)


class OrderStatusView(PublicAPIView):
    throttle_scope = "shop_status"

    def get(self, request, reference):
        order = _get_order(reference, request.query_params.get("token"))
        return Response(OrderPublicSerializer(order).data)


class OrderPayView(PublicAPIView):
    """Retry the STK push (customer dismissed the prompt, wrong PIN, timeout...)."""
    throttle_scope = "shop_checkout"

    def post(self, request, reference):
        order = _get_order(reference, request.data.get("token"))
        if order.status != Order.PENDING_PAYMENT or order.payment_status == Order.PAY_PAID:
            return Response({"detail": "This order can no longer be paid."}, status=400)
        if order.payment_method != Order.METHOD_MPESA:
            return Response({"detail": "Unsupported payment method."}, status=400)
        if (order.payment_status == Order.PAY_PENDING and order.last_stk_at
                and timezone.now() - order.last_stk_at < timedelta(seconds=90)):
            return Response({"detail": "A payment prompt was just sent. Please approve it on your phone or wait a minute."},
                            status=429)
        phone = normalize_phone_number(request.data.get("phone") or order.mpesa_phone or order.phone)
        if not is_valid_phone_number(phone):
            return Response({"detail": "Enter a valid Kenyan M-Pesa number."}, status=400)
        ok, message = initiate_mpesa_payment(order, phone)
        return Response({"initiated": ok, "message": message},
                        status=200 if ok else status.HTTP_502_BAD_GATEWAY)


class PaymentCallbackView(PublicAPIView):
    """Safaricom STK callback. Always answers 200 so Safaricom doesn't retry; the reconciler is the safety net."""

    def post(self, request, secret):
        expected = getattr(settings, "SHOP_MPESA_CALLBACK_SECRET", "")
        if not expected or not constant_time_compare(secret, expected):
            raise Http404
        try:
            cb = request.data["Body"]["stkCallback"]
            checkout_id = cb["CheckoutRequestID"]
            code = int(cb["ResultCode"])
        except (KeyError, TypeError, ValueError):
            return Response({"ResultCode": 0, "ResultDesc": "Ignored"})

        receipt, amount = "", None
        meta = cb.get("CallbackMetadata") or {}
        for item in (meta.get("Item") or []):
            if item.get("Name") == "MpesaReceiptNumber":
                receipt = str(item.get("Value") or "")
            elif item.get("Name") == "Amount":
                amount = item.get("Value")
        try:
            apply_payment_result(checkout_id, success=(code == 0),
                                 desc=str(cb.get("ResultDesc", "")), receipt=receipt, amount=amount)
        except Exception:
            logger.exception("Shop callback processing failed checkout=%s", checkout_id)
        return Response({"ResultCode": 0, "ResultDesc": "Accepted"})


# ───────────── Superadmin API ─────────────
class ShopPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 200


ORDER_TRANSITIONS = {
    Order.PENDING_PAYMENT: {Order.CANCELLED},
    Order.PAID: {Order.PROCESSING, Order.CANCELLED},
    Order.PROCESSING: {Order.SHIPPED, Order.CANCELLED},
    Order.SHIPPED: {Order.DELIVERED},
}


class AdminCategoryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsSuperAdmin]
    serializer_class = AdminCategorySerializer
    queryset = Category.objects.all()
    pagination_class = ShopPagination


class AdminProductViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, IsSuperAdmin]
    serializer_class = AdminProductSerializer
    lookup_field = "slug"
    pagination_class = ShopPagination

    def get_queryset(self):
        qs = Product.objects.select_related("category").order_by("sort_order", "name")
        p = self.request.query_params
        if p.get("category"):
            qs = qs.filter(category_id=p["category"]) if p["category"].isdigit() else qs.filter(category__slug=p["category"])
        if p.get("search"):
            qs = qs.filter(Q(name__icontains=p["search"][:80]) | Q(slug__icontains=p["search"][:80]))
        if p.get("is_active") in ("true", "false"):
            qs = qs.filter(is_active=p["is_active"] == "true")
        return qs


class AdminOrderViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated, IsSuperAdmin]
    lookup_field = "reference"
    pagination_class = ShopPagination

    def get_serializer_class(self):
        return AdminOrderDetailSerializer if self.action == "retrieve" else AdminOrderListSerializer

    def get_queryset(self):
        qs = Order.objects.all()
        if self.action == "retrieve":
            qs = qs.prefetch_related("items")
        p = self.request.query_params
        if p.get("status"):
            qs = qs.filter(status=p["status"])
        if p.get("payment_status"):
            qs = qs.filter(payment_status=p["payment_status"])
        s = (p.get("search") or "").strip()[:80]
        if s:
            qs = qs.filter(Q(reference__icontains=s) | Q(email__icontains=s) | Q(phone__icontains=s)
                           | Q(last_name__icontains=s) | Q(mpesa_receipt__icontains=s))
        return qs

    @action(detail=True, methods=["post"], url_path="status")
    def set_status(self, request, reference=None):
        new = request.data.get("status")
        with transaction.atomic():
            order = get_object_or_404(Order.objects.select_for_update(), reference=reference)
            if new not in ORDER_TRANSITIONS.get(order.status, set()):
                return Response({"detail": f"Cannot move order from '{order.status}' to '{new}'."}, status=400)
            if new == Order.CANCELLED:
                release_stock(order)
            order.status = new
            order.save(update_fields=["status", "stock_released", "updated_at"])
        return Response(AdminOrderListSerializer(order).data)

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, reference=None):
        """Manual confirmation (callback lost AND STK query inconclusive)."""
        with transaction.atomic():
            order = get_object_or_404(Order.objects.select_for_update(), reference=reference)
            if order.payment_status == Order.PAY_PAID:
                return Response({"detail": "Already paid."}, status=400)
            mark_order_paid(order, str(request.data.get("receipt", ""))[:30])
        return Response(AdminOrderListSerializer(order).data)

    @action(detail=False, methods=["get"])
    def stats(self, request):
        paid = Q(payment_status=Order.PAY_PAID)
        since = timezone.now() - timedelta(days=30)
        agg = Order.objects.aggregate(
            total=Count("id"),
            pending=Count("id", filter=Q(status=Order.PENDING_PAYMENT)),
            paid=Count("id", filter=paid),
            to_fulfil=Count("id", filter=Q(status__in=[Order.PAID, Order.PROCESSING])),
            revenue_usd=Sum("total", filter=paid),
            revenue_kes=Sum("amount_kes", filter=paid),
            revenue_usd_30d=Sum("total", filter=paid & Q(paid_at__gte=since)),
        )
        return Response({k: (float(v) if v is not None and k.startswith("revenue") else (v or 0)) for k, v in agg.items()})