from datetime import date, datetime, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.subscriptions.tasks import _invoice_reminder_milestone, _invoice_reminder_target_date


class SubscriptionInvoiceReminderDateTests(SimpleTestCase):
    def test_future_service_expiry_wins_over_past_invoice_due_date(self):
        invoice = SimpleNamespace(due_date=date(2026, 11, 1))
        subscription = SimpleNamespace(current_period_end=datetime(2026, 11, 9, tzinfo=timezone.utc))
        tenant = SimpleNamespace(subscription_expiry=date(2026, 11, 9))
        cycle = SimpleNamespace(
            end_date=datetime(2026, 11, 9, tzinfo=timezone.utc),
            grace_ends_at=None,
            subscription=subscription,
            tenant=tenant,
        )

        self.assertEqual(_invoice_reminder_target_date(invoice, cycle), date(2026, 11, 9))
        self.assertEqual(_invoice_reminder_milestone(invoice, cycle, date(2026, 11, 4), {5, 3, 1}, True), "5")

    def test_expired_notice_only_after_latest_authoritative_date(self):
        invoice = SimpleNamespace(due_date=date(2026, 11, 1))
        subscription = SimpleNamespace(current_period_end=datetime(2026, 11, 9, tzinfo=timezone.utc))
        tenant = SimpleNamespace(subscription_expiry=date(2026, 11, 9))
        cycle = SimpleNamespace(
            end_date=datetime(2026, 11, 9, tzinfo=timezone.utc),
            grace_ends_at=None,
            subscription=subscription,
            tenant=tenant,
        )

        self.assertIsNone(_invoice_reminder_milestone(invoice, cycle, date(2026, 11, 8), {5, 3}, True))
        self.assertEqual(_invoice_reminder_milestone(invoice, cycle, date(2026, 11, 10), {5, 3}, True), "expired")
