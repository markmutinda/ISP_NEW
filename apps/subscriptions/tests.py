from django.test import TestCase
from rest_framework.test import APIRequestFactory

from apps.subscriptions.models import NetilyPlan
from apps.subscriptions.views import BillingCalculatorView


class BillingCalculatorViewTests(TestCase):
    def setUp(self):
        NetilyPlan.objects.create(
            name="Starter",
            code="starter",
            tagline="Flat plan",
            is_active=True,
            is_metered=False,
            price_monthly="2500.00",
            price_yearly="25000.00",
            base_license_fee="2500.00",
            pppoe_unit_price="0.00",
            pppoe_min_clients=0,
            hotspot_revenue_share_pct="0.00",
            sort_order=1,
            features=["Support"],
        )
        NetilyPlan.objects.create(
            name="Metered",
            code="metered",
            tagline="Usage-based",
            is_active=True,
            is_metered=True,
            price_monthly="0.00",
            price_yearly="0.00",
            base_license_fee="1000.00",
            pppoe_unit_price="10.00",
            pppoe_min_clients=20,
            hotspot_revenue_share_pct="5.00",
            sort_order=2,
            features=["Automation"],
        )
        self.factory = APIRequestFactory()

    def test_get_supports_query_parameters(self):
        request = self.factory.get(
            "/api/v1/subscriptions/calculator/",
            {"pppoe_clients": 50, "monthly_hotspot_revenue": 10000},
        )
        response = BillingCalculatorView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        data = response.data
        metered = next(item for item in data if item["plan_code"] == "metered")
        self.assertEqual(metered["input_pppoe_clients"], 50)
        self.assertEqual(str(metered["input_hotspot_revenue"]), "10000")
        self.assertEqual(str(metered["estimated_monthly"]), "2000.00")
