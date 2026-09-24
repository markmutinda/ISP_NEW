from contextlib import nullcontext
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from .billing_lifecycle import complete_subscription_stk_payment
from .views import SubscriptionPaymentViewSet


class PaymentUnlockTests(SimpleTestCase):
    def setUp(self):
        self.subscription = MagicMock(
            id='sub', status='past_due', is_trial=False,
            current_period_start=timezone.now() - timedelta(days=31),
            current_period_end=timezone.now() - timedelta(days=1),
        )
        self.payment = MagicMock(
            id='payment', pk='payment', subscription_id='sub',
            status='pending', activation_applied_at=None, mpesa_receipt='',
            completed_at=None, period_end=None, subscription=self.subscription,
        )

    def complete(self, invoice):
        with (
            patch('apps.subscriptions.billing_lifecycle.schema_context', return_value=nullcontext()),
            patch('django.db.transaction.atomic', return_value=nullcontext()),
            patch('django.db.transaction.on_commit') as on_commit,
            patch('apps.subscriptions.models.SubscriptionPayment.objects') as payments,
            patch('apps.subscriptions.models.CompanySubscription.objects') as subscriptions,
            patch('apps.subscriptions.billing_lifecycle.get_tenant_for_subscription', return_value=None),
            patch('apps.subscriptions.billing_lifecycle.sync_subscription_invoice_payment', return_value=invoice) as sync,
        ):
            payments.select_for_update.return_value.get.return_value = self.payment
            subscriptions.select_for_update.return_value.get.return_value = self.subscription
            complete_subscription_stk_payment(self.payment, 'RECEIPT')
            return sync, on_commit

    def test_full_payment_renews_before_notifications_are_queued(self):
        sync, on_commit = self.complete(SimpleNamespace(pk=1, balance=Decimal('0'), status='PAID'))
        sync.assert_called_once_with(self.payment, notify=False)
        self.subscription.extend_subscription.assert_called_once()
        self.assertIsNotNone(self.payment.activation_applied_at)
        on_commit.assert_called_once()

    def test_partial_payment_does_not_renew(self):
        self.complete(SimpleNamespace(pk=1, balance=Decimal('250'), status='PARTIAL'))
        self.subscription.extend_subscription.assert_not_called()
        self.assertIsNone(self.payment.activation_applied_at)

    def test_duplicate_confirmation_does_not_extend_again(self):
        self.payment.activation_applied_at = timezone.now()
        sync, on_commit = self.complete(None)
        sync.assert_not_called()
        on_commit.assert_not_called()
        self.subscription.extend_subscription.assert_not_called()

    def test_completed_but_unactivated_payment_is_repaired(self):
        self.payment.status = 'completed'
        self.payment.completed_at = timezone.now()
        self.complete(SimpleNamespace(pk=1, balance=Decimal('0'), status='PAID'))
        self.payment.mark_completed.assert_not_called()
        self.subscription.extend_subscription.assert_called_once()

    def test_paid_target_invoice_activates_even_with_unrelated_old_invoice(self):
        tenant = SimpleNamespace(schema_name='tenant')
        with (
            patch('apps.subscriptions.billing_lifecycle.schema_context', return_value=nullcontext()),
            patch('django.db.transaction.atomic', return_value=nullcontext()),
            patch('django.db.transaction.on_commit'),
            patch('apps.subscriptions.models.SubscriptionPayment.objects') as payments,
            patch('apps.subscriptions.models.CompanySubscription.objects') as subscriptions,
            patch('apps.subscriptions.billing_lifecycle.get_tenant_for_subscription', return_value=tenant),
            patch(
                'apps.subscriptions.billing_lifecycle.sync_subscription_invoice_payment',
                return_value=SimpleNamespace(pk=1, balance=Decimal('0'), status='PAID'),
            ),
        ):
            payments.select_for_update.return_value.get.return_value = self.payment
            subscriptions.select_for_update.return_value.get.return_value = self.subscription
            complete_subscription_stk_payment(self.payment, 'RECEIPT')
        self.subscription.extend_subscription.assert_called_once()

    def test_old_payment_does_not_renew_a_later_expired_cycle(self):
        self.payment.status = 'completed'
        self.payment.completed_at = timezone.now() - timedelta(days=60)
        sync, _ = self.complete(None)
        sync.assert_not_called()
        self.subscription.extend_subscription.assert_not_called()

    def test_pending_payment_cannot_report_activation_for_early_renewal(self):
        self.subscription.status = 'active'
        self.subscription.current_period_end = timezone.now() + timedelta(days=3)
        view = SubscriptionPaymentViewSet()
        with (
            patch('apps.subscriptions.views.schema_context', return_value=nullcontext()),
            patch.object(view, 'get_object', return_value=self.payment),
            patch.object(view, '_reconcile_gateway_status', return_value=self.payment),
            patch.object(view, '_billing_cycle_payload', return_value={}),
        ):
            response = view.status(APIRequestFactory().get('/'))
        self.assertFalse(response.data['subscription_activated'])
        self.assertFalse(response.data['payment_received'])

    def test_billing_response_disables_caching(self):
        from rest_framework.response import Response
        view = SubscriptionPaymentViewSet()
        view.headers = {}
        request = APIRequestFactory().get('/')
        view.format_kwarg = None
        response = view.finalize_response(request, Response({}))
        self.assertIn('no-store', response['Cache-Control'])

    def test_late_gateway_failure_only_updates_unconfirmed_payments(self):
        self.payment.payhero_checkout_id = 'checkout'
        view = SubscriptionPaymentViewSet()
        with (
            patch('apps.subscriptions.payment_recovery.cache.add', return_value=True),
            patch('apps.subscriptions.payment_recovery.query_stk_status', return_value={'ResultCode': 1032}),
            patch('apps.subscriptions.models.SubscriptionPayment.objects') as payments,
        ):
            view._reconcile_gateway_status(self.payment)
            payments.filter.assert_called_once_with(pk=self.payment.pk, status__in=['pending', 'processing'])
        self.payment.mark_failed.assert_not_called()

    def test_gateway_success_recovery_uses_shared_lifecycle(self):
        self.payment.payhero_checkout_id = 'checkout'
        view = SubscriptionPaymentViewSet()
        with (
            patch('apps.subscriptions.payment_recovery.cache.add', return_value=True),
            patch('apps.subscriptions.payment_recovery.query_stk_status', return_value={'ResultCode': 0}),
            patch('apps.subscriptions.payment_recovery.complete_subscription_stk_payment', return_value=(self.payment, None)) as complete,
        ):
            view._reconcile_gateway_status(self.payment)
            complete.assert_called_once_with(self.payment, mpesa_receipt='')

    def test_notification_broker_failure_is_isolated(self):
        from .billing_lifecycle import _queue_completed_subscription_notice
        with (
            patch('apps.subscriptions.tasks.send_subscription_payment_receipt.apply_async', side_effect=RuntimeError('offline')),
            self.assertLogs('apps.subscriptions.billing_lifecycle', level='ERROR'),
        ):
            _queue_completed_subscription_notice('payment', 'invoice')

    def test_callback_failure_is_not_acknowledged_as_saved(self):
        from .webhook_views import SubscriptionPaybillCallbackView
        request = SimpleNamespace(data={'CheckoutRequestID': 'checkout', 'ResultCode': 0})
        with (
            patch('apps.subscriptions.webhook_views._find_subscription_payment', return_value=self.payment),
            patch('apps.subscriptions.webhook_views.complete_subscription_stk_payment', side_effect=RuntimeError('database offline')),
            self.assertLogs('apps.subscriptions.webhook_views', level='ERROR'),
        ):
            response = SubscriptionPaybillCallbackView()._process_callback(request)
        self.assertEqual(response.status_code, 503)

    def test_hotspot_revenue_recorder_uses_atomic_f_expression(self):
        from apps.billing.tasks import record_hotspot_revenue

        tenant = SimpleNamespace(schema_name='tenant')
        cycle = SimpleNamespace(id='cycle')
        with (
            patch('django_tenants.utils.schema_context', return_value=nullcontext()),
            patch('apps.core.models.Tenant.objects.get', return_value=tenant),
            patch('apps.subscriptions.models.BillingCycle.objects') as cycles,
        ):
            cycles.filter.return_value.first.return_value = cycle
            record_hotspot_revenue('tenant', '125.50')
        update_kwargs = cycles.filter.return_value.update.call_args.kwargs
        self.assertIn('hotspot_revenue_accumulated', update_kwargs)
        self.assertNotIsInstance(update_kwargs['hotspot_revenue_accumulated'], Decimal)

    def test_billing_cycle_hotspot_revenue_uses_reports_source(self):
        from apps.core.models import Company, Tenant
        from .models import BillingCycle

        now = timezone.now()
        company = Company(
            name='Tenant',
            slug='tenant',
            email='tenant@example.com',
            phone_number='0700000000',
            address='Nairobi',
            city='Nairobi',
        )
        tenant = Tenant(
            company=company,
            subdomain='tenant',
            database_name='tenant',
            schema_name='tenant',
        )
        cycle = BillingCycle(
            tenant=tenant,
            start_date=now - timedelta(days=30),
            end_date=now,
        )
        reports_style_total = {
            "revenue": Decimal("34740.00"),
            "count": 12,
            "source": "completed_hotspot_payments",
        }

        with (
            patch('django_tenants.utils.schema_context', return_value=nullcontext()),
            patch(
                'apps.billing.services.hotspot_revenue.rolling_reports_hotspot_payment_revenue',
                return_value=reports_style_total,
            ) as revenue_source,
        ):
            details = cycle.get_actual_hotspot_revenue_details()

        self.assertEqual(details, reports_style_total)
        revenue_source.assert_called_once_with(cycle)

    def test_reports_hotspot_revenue_window_caps_long_cycles_to_rolling_30d(self):
        from apps.billing.services.hotspot_revenue import rolling_reports_hotspot_payment_revenue

        now = timezone.now()
        cycle = SimpleNamespace(
            start_date=now - timedelta(days=45),
            end_date=now + timedelta(days=15),
        )

        with (
            patch('apps.billing.services.hotspot_revenue.timezone.now', return_value=now),
            patch(
                'apps.billing.services.hotspot_revenue.completed_hotspot_payment_revenue',
                return_value={
                    "revenue": Decimal("34760.00"),
                    "count": 2700,
                    "source": "completed_hotspot_payments",
                },
            ) as revenue_source,
        ):
            details = rolling_reports_hotspot_payment_revenue(cycle)

        start, end = revenue_source.call_args.args
        self.assertEqual(start, now - timedelta(days=30))
        self.assertEqual(end, now)
        self.assertEqual(details["revenue"], Decimal("34760.00"))

    def test_reports_hotspot_revenue_window_does_not_include_before_new_cycle(self):
        from apps.billing.services.hotspot_revenue import rolling_reports_hotspot_payment_revenue

        now = timezone.now()
        cycle = SimpleNamespace(
            start_date=now - timedelta(days=10),
            end_date=now + timedelta(days=20),
        )

        with (
            patch('apps.billing.services.hotspot_revenue.timezone.now', return_value=now),
            patch(
                'apps.billing.services.hotspot_revenue.completed_hotspot_payment_revenue',
                return_value={
                    "revenue": Decimal("9000.00"),
                    "count": 900,
                    "source": "completed_hotspot_payments",
                },
            ) as revenue_source,
        ):
            details = rolling_reports_hotspot_payment_revenue(cycle)

        start, end = revenue_source.call_args.args
        self.assertEqual(start, cycle.start_date)
        self.assertEqual(end, now)
        self.assertEqual(details["revenue"], Decimal("9000.00"))
