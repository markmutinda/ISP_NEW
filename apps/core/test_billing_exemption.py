from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.core.hotspot_lock import _compute
from apps.core.middleware import SubscriptionEnforcementMiddleware


class BillingExemptionTests(SimpleTestCase):
    def test_billing_exempt_tenant_bypasses_subscription_enforcement(self):
        tenant = SimpleNamespace(status='active', is_billing_exempt=True)
        request = SimpleNamespace(path='/api/v1/core/dashboard/', tenant=tenant, company=object(), user=None)
        self.assertIsNone(SubscriptionEnforcementMiddleware(lambda request: None).process_request(request))

    @patch('apps.subscriptions.models.CompanySubscription.objects')
    @patch('apps.core.hotspot_lock.schema_context')
    def test_billing_exempt_tenant_never_hotspot_locks(self, schema_context, subscriptions):
        schema_context.return_value.__enter__.return_value = None
        subscriptions.filter.return_value.values.return_value.first.return_value = {
            'status': 'past_due', 'current_period_end': None, 'trial_ends_at': None,
            'company__phone_number': '', 'company__name': 'Demo ISP',
            'company__tenant__is_billing_exempt': True,
        }
        self.assertEqual(_compute('demo'), {'locked': False, 'billing_exempt': True})
