from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.core.models import Company, Tenant
from apps.subscriptions.models import CompanySubscription, NetilyPlan
from apps.superadmin.serializers import PlatformExpenditureSerializer, TenantListSerializer
from apps.superadmin.views import DashboardView, PlatformExpenditureView, SubscriptionPaymentListView


class PlatformExpenditureLedgerTests(SimpleTestCase):
    def test_receipts_after_boundary_belong_to_new_account(self):
        cutover = datetime.fromisoformat("2026-09-29T16:39:20+03:00")
        with patch.object(PlatformExpenditureView, "_cutover_payment") as payment:
            payment.return_value.completed_at = cutover
            view = SubscriptionPaymentListView()
            self.assertEqual(view._account_for_paid_at(cutover), "primary")
            self.assertEqual(view._account_for_paid_at(cutover + timedelta(seconds=1)), "new_business")

    def test_signed_expenditure_allows_credit_but_not_zero(self):
        serializer = PlatformExpenditureSerializer()
        self.assertEqual(serializer.validate_amount(Decimal("-6212.00")), Decimal("-6212.00"))
        with self.assertRaises(Exception):
            serializer.validate_amount(Decimal("0.00"))

    def test_cutover_looks_up_fixed_receipt(self):
        view = PlatformExpenditureView()
        with patch("apps.subscriptions.models.SubscriptionPayment.objects") as payments:
            view._cutover_payment()
        lookup = payments.select_related.return_value.filter.return_value.filter.call_args.args[0]
        self.assertIn("ACCD33A971EF0", str(lookup))
        self.assertNotIn("bentrex", str(lookup).lower())

    def test_explicit_manual_account_takes_precedence_over_date(self):
        view = PlatformExpenditureView()
        view.ledger_key = "new_business"
        with patch("apps.subscriptions.models.SubscriptionPayment.objects") as payments:
            qs = MagicMock()
            payments.filter.return_value = qs
            qs.filter.return_value = qs
            qs.aggregate.return_value = {"total": Decimal("6212.00")}
            self.assertEqual(view._subscription_total(None, None, datetime.fromisoformat("2026-09-29T16:39:59+03:00")), Decimal("6212.00"))
        account_filter = str(qs.filter.call_args.args[0])
        self.assertIn("new_business", account_filter)
        self.assertIn("business_account__isnull", account_filter)


class CompanySubscriptionBillingAnchorTests(SimpleTestCase):
    def test_monthly_period_keeps_anchor_day_after_short_month(self):
        start = timezone.make_aware(datetime(2026, 1, 31, 9, 0))
        subscription = CompanySubscription(
            billing_period="monthly",
            current_period_start=start,
            current_period_end=start,
            billing_anchor_day=31,
        )

        feb_end = subscription.next_period_end(start)
        mar_end = subscription.next_period_end(feb_end)

        self.assertEqual(feb_end.date().isoformat(), "2026-02-28")
        self.assertEqual(mar_end.date().isoformat(), "2026-03-31")

    def test_monthly_period_keeps_regular_paid_day(self):
        start = timezone.make_aware(datetime(2026, 9, 12, 14, 30))
        subscription = CompanySubscription(
            billing_period="monthly",
            current_period_start=start,
            current_period_end=start,
            billing_anchor_day=12,
        )

        self.assertEqual(subscription.next_period_end(start).date().isoformat(), "2026-10-12")


class TenantListSerializerTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Green Network",
            slug="green-network",
            email="admin@netily.io",
            phone_number="+254700123456",
            address="Nairobi",
            city="Nairobi",
            company_type="isp",
        )
        self.plan = NetilyPlan.objects.create(
            name="Starter",
            code="starter",
            price_monthly=Decimal("0.00"),
            price_yearly=Decimal("0.00"),
            max_subscribers=0,
            max_routers=0,
            max_staff=0,
        )

    def test_serializer_uses_company_subscription_expiry_and_display_status(self):
        trial_end = timezone.now() + timedelta(days=9)
        CompanySubscription.objects.create(
            company=self.company,
            plan=self.plan,
            billing_period="monthly",
            current_period_start=timezone.now(),
            current_period_end=timezone.now() + timedelta(days=30),
            status="trialing",
            is_trial=True,
            trial_started_at=timezone.now(),
            trial_ends_at=trial_end,
        )
        tenant = Tenant(
            company=self.company,
            subdomain="green",
            schema_name="green",
            database_name="green",
            status="trial",
            subscription_expiry=None,
        )

        data = TenantListSerializer(instance=tenant).data

        self.assertEqual(data["subscription_expiry"], trial_end.date())
        self.assertEqual(data["subscription_status"], "Trial")
        self.assertEqual(data["subscription_status_code"], "trialing")
        self.assertEqual(data["tenant_status_display"], "Trial")

    def test_dashboard_plan_name_uses_company_subscription(self):
        CompanySubscription.objects.create(
            company=self.company,
            plan=self.plan,
            billing_period="monthly",
            current_period_start=timezone.now(),
            current_period_end=timezone.now() + timedelta(days=30),
            status="active",
        )
        tenant = Tenant(
            company=self.company,
            subdomain="green",
            schema_name="green",
            database_name="green",
            status="active",
        )

        self.assertEqual(DashboardView()._tenant_subscription_plan_name(tenant), "Starter")
