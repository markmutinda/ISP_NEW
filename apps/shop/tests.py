from unittest.mock import patch

from django.test import TestCase
from rest_framework.exceptions import ValidationError

from apps.shop.models import Category, Order, Product
from apps.shop.services.orders import create_order
from apps.shop.services.payments import apply_payment_result


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
        self.assertGreater(order.amount_kes, 0)

    def test_oversell_rejected_and_rolled_back(self):
        with self.assertRaises(ValidationError):
            create_order(_payload("r1", 6))
        self.p.refresh_from_db()
        self.assertEqual(self.p.stock_quantity, 5)
        self.assertEqual(Order.objects.count(), 0)

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