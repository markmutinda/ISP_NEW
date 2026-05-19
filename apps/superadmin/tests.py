from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.core.models import Company, Tenant
from apps.subscriptions.models import CompanySubscription, NetilyPlan
from apps.superadmin.serializers import TenantListSerializer


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
