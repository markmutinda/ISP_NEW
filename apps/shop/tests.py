from unittest.mock import patch

from django.test import TestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIRequestFactory

from apps.shop.models import Category, Order, Product, ShopSettings
from apps.shop.services.orders import create_order
from apps.shop.services.payments import apply_payment_result
from apps.shop.views import CategoryListView, ProductListView


def _payload(slug, qty):
    return dict(first_name="A", last_name="B", email="a@b.com", phone="0712345678", address="x",
                city="Nairobi", payment_method="mpesa", items=[{"product_id": slug, "quantity": qty}])


class ShopOrderTests(TestCase):
    def setUp(self):
        cat = Category.objects.create(name="Routers", slug="routers")
        self.p = Product.objects.create(slug="r1", name="R1", category=cat, price=100, stock_quantity=5)

    def test_server_side_price_and_stock_reservation(self):
        order = create_order(_payload("r1", 2))
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock_quantity, 3)
        self.assertEqual(float(order.total), 200.0)
        self.assertEqual(order.currency, "KES")
        self.assertEqual(order.amount_kes, 200)
        self.assertEqual(float(order.fx_rate), 1.0)

    def test_oversell_rejected_and_rolled_back(self):
        with self.assertRaises(ValidationError):
            create_order(_payload("r1", 6))
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock_quantity, 5)
        self.assertEqual(Order.objects.count(), 0)

    def test_managed_shipping_fee_and_free_shipping_threshold(self):
        settings = ShopSettings.get_active()
        settings.default_shipping_fee = 300
        settings.free_shipping_threshold = 500
        settings.save()

        charged = create_order(_payload("r1", 2))
        self.assertEqual(float(charged.shipping_fee), 300.0)
        self.assertEqual(float(charged.total), 500.0)

        self.p.stock_quantity = 5
        self.p.save(update_fields=["stock_quantity"])
        free = create_order(_payload("r1", 5))
        self.assertEqual(float(free.shipping_fee), 0.0)
        self.assertEqual(float(free.total), 500.0)

    def test_callback_idempotent_and_amount_checked(self):
        order = create_order(_payload("r1", 1))
        Order.objects.filter(pk=order.pk).update(checkout_request_id="ws_CO_1")
        apply_payment_result("ws_CO_1", success=True, receipt="ABC123", amount=1)  # underpaid
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "pending")
        with patch("apps.shop.services.payments._queue_confirmation"):
            apply_payment_result("ws_CO_1", success=True, receipt="ABC123", amount=order.amount_kes)
            apply_payment_result("ws_CO_1", success=True, receipt="ABC123", amount=order.amount_kes)
        order.refresh_from_db()
        self.assertEqual((order.status, order.payment_status), ("paid", "paid"))


class ShopDiscoveryTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name="Routers & Gateways",
            slug="routers-gateways",
            description="Core and edge routing.",
            featured_brands=["MikroTik", "Ubiquiti"],
        )
        Product.objects.create(
            slug="router-sale",
            name="Router Sale",
            brand="MikroTik",
            category=self.category,
            price=100,
            compare_at_price=120,
            stock_quantity=2,
            is_trending=True,
        )
        Product.objects.create(
            slug="router-hidden",
            name="Hidden Router",
            brand="Ubiquiti",
            category=self.category,
            price=200,
            stock_quantity=0,
            is_active=False,
        )
        self.factory = APIRequestFactory()

    def test_categories_include_merchandising_and_live_count(self):
        response = CategoryListView.as_view()(self.factory.get("/shop/categories/"))
        self.assertEqual(response.data[0]["product_count"], 1)
        self.assertEqual(response.data[0]["featured_brands"], ["MikroTik", "Ubiquiti"])

    def test_catalog_filters_brand_sale_trending_and_stock(self):
        request = self.factory.get(
            "/shop/products/",
            {"brand": "MikroTik", "on_sale": "true", "trending": "true", "in_stock": "true"},
        )
        response = ProductListView.as_view()(request)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], "router-sale")
