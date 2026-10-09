from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.core.models import Company, Tenant
from apps.subscriptions.models import CompanySubscription, NetilyPlan
from apps.superadmin.serializers import PlatformExpenditureSerializer, TenantListSerializer
from apps.superadmin.views import DashboardView, PlatformExpenditureView, SubscriptionPaymentListView
from apps.superadmin.management.commands.record_business_account_transfer import Command as RecordTransferCommand
from apps.superadmin.financial_csv import FinancialCSVView, _amount, _csv_response, _paid_at, _safe_cell, _sms_reference
from django.core.files.uploadedfile import SimpleUploadedFile


class FinancialCSVTests(SimpleTestCase):
    def test_text_cells_are_safe_for_spreadsheets(self):
        self.assertEqual(_safe_cell("=HYPERLINK(\"https://example.com\")"), "'=HYPERLINK(\"https://example.com\")")
        self.assertEqual(_safe_cell("Normal entry"), "Normal entry")
        self.assertEqual(_amount("12.345"), None)
        self.assertIsNone(_paid_at("2026-02-31"))
        self.assertEqual(_sms_reference(SimpleNamespace(payment_reference="", notes="Receipt: UJT123", checkout_request_id="checkout")), "UJT123")

    def test_export_has_header_and_preserves_signed_numeric_amount(self):
        response = _csv_response("expenditure", [("2026-10-09", "operations", "=unsafe", Decimal("-10.00"), "credit")])
        body = response.content.decode("utf-8-sig")
        self.assertIn("date,category,title,amount,notes", body)
        self.assertIn("'=unsafe,-10.00,credit", body)

    def test_preview_reports_duplicate_rows_without_saving(self):
        content = b"date,category,title,amount,notes\n2026-10-09,operations,Hosting,12.00,\n2026-10-09,operations,Hosting,12.00,\n"
        file = SimpleUploadedFile("expense.csv", content, content_type="text/csv")
        request = SimpleNamespace(FILES={"file": file}, data={}, user=SimpleNamespace(), META={})
        with patch("apps.superadmin.financial_csv._ensure_public"), patch.object(
            FinancialCSVView, "_check_expenditure", return_value=({"title": "Hosting"}, ("primary", "Hosting", "12.00"))
        ):
            response = FinancialCSVView().post(request, "expenditure")
        self.assertEqual(response.data["valid"], 1)
        self.assertEqual(response.data["invalid"], 1)
        self.assertIn("Duplicate row", response.data["rows"][1]["errors"][0])


class PlatformExpenditureLedgerTests(SimpleTestCase):
    def test_transfer_changes_calculated_positions_but_not_receipts(self):
        request = SimpleNamespace(query_params={})
        manual = MagicMock()
        manual.aggregate.return_value = {"total": Decimal("11422.00")}
        manual.count.return_value = 0
        manual.__getitem__.return_value = []
        with patch("apps.superadmin.views._ensure_public"), patch.object(
            PlatformExpenditureView, "_cutover_payment", return_value=None
        ), patch.object(PlatformExpenditureView, "_manual_qs", return_value=manual), patch.object(
            PlatformExpenditureView, "_subscription_total", return_value=Decimal("10891.70")
        ), patch.object(PlatformExpenditureView, "_sms_topup_total", return_value=Decimal("504.60")), patch.object(
            PlatformExpenditureView, "_transfer_total", return_value=Decimal("18000.00")
        ):
            view = PlatformExpenditureView()
            view.ledger_key = "new_business"
            summary = view.get(request).data["summary"]
        self.assertEqual(summary["accrued_total"], "11396.30")
        self.assertEqual(summary["net_profit"], "-25.70")
        self.assertEqual(summary["transfer_in_total"], "18000.00")
        self.assertEqual(summary["calculated_position"], "17974.30")

    def test_record_transfer_command_does_not_write_without_apply(self):
        with patch("apps.superadmin.management.commands.record_business_account_transfer.schema_context"), patch(
            "apps.superadmin.management.commands.record_business_account_transfer.get_public_schema_name", return_value="public"
        ), patch("apps.superadmin.management.commands.record_business_account_transfer.BusinessAccountTransfer.objects") as transfers:
            RecordTransferCommand().handle(
                amount="18000", effective_at="2026-09-06T23:29:00+03:00",
                reference="CUTOVER-TRANSFER", apply=False,
            )
        transfers.get_or_create.assert_not_called()

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
