from decimal import Decimal

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.models import EmailOTP, LoginOTPChallenge, User

from .models import AffiliateAccount, AffiliateClick, AffiliatePayout, AffiliateReferral
from .services import record_affiliate_signup


@override_settings(
    CORS_ALLOW_ALL_ORIGINS=False,
    CORS_ALLOWED_ORIGINS=["https://netily.co.ke"],
)
class AffiliateCorsTests(SimpleTestCase):
    def test_login_preflight_allows_browser_session_header(self):
        response = self.client.options(
            "/api/v1/affiliate/login/",
            HTTP_ORIGIN="https://netily.co.ke",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type,x-session-id",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "https://netily.co.ke")
        allowed_headers = response["Access-Control-Allow-Headers"].lower().split(", ")
        self.assertIn("x-session-id", allowed_headers)


class AffiliateApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="affiliate@example.com",
            password="Strong-Test-Pass-349!",
            phone_number="+254700001001",
            first_name="Amina",
            last_name="Partner",
            is_active=True,
        )
        self.account = AffiliateAccount.objects.create(
            user=self.user,
            country="Kenya",
            currency="KES",
            referral_code="AMINA123",
            is_verified=True,
        )

    def test_active_affiliate_can_login_and_read_own_profile(self):
        response = self.client.post(
            "/api/v1/affiliate/login/",
            {"email": self.user.email, "password": "Strong-Test-Pass-349!"},
            format="json",
            HTTP_X_SESSION_ID="affiliate-browser-1",
        )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.data["requires_otp"])
        challenge = LoginOTPChallenge.objects.get(pk=response.data["challenge_id"])
        self.assertEqual(challenge.tenant_scope, "public:affiliate")
        otp = EmailOTP.objects.get(login_challenge=challenge, is_used=False)

        response = self.client.post(
            "/api/v1/affiliate/login/",
            {
                "email": self.user.email,
                "password": "Strong-Test-Pass-349!",
                "challenge_id": str(challenge.id),
                "otp_code": otp.code,
            },
            format="json",
            HTTP_X_SESSION_ID="affiliate-browser-1",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertEqual(response.data["user"]["referral_code"], "AMINA123")
        challenge.refresh_from_db()
        self.assertTrue(challenge.is_completed)

        self.client.force_authenticate(self.user)
        response = self.client.get("/api/v1/affiliate/me/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["email"], self.user.email)

    def test_suspended_affiliate_is_denied(self):
        self.account.status = "suspended"
        self.account.save(update_fields=["status"])
        response = self.client.post(
            "/api/v1/affiliate/login/",
            {"email": self.user.email, "password": "Strong-Test-Pass-349!"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get("/api/v1/affiliate/dashboard/").status_code, 403)

    def test_click_and_signup_are_automatic_but_commission_is_not(self):
        response = self.client.post(
            "/api/v1/affiliate/r/AMINA123/click/",
            {"source": "WhatsApp", "landing_url": "https://netily.co.ke/"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        click = AffiliateClick.objects.get(affiliate=self.account)

        referral = record_affiliate_signup(
            referral_code="AMINA123",
            attribution_token=str(click.attribution_token),
            email="newisp@example.com",
            company_name="New ISP",
        )
        self.assertEqual(referral.click, click)
        self.assertEqual(referral.status, "pending")
        self.assertEqual(referral.reward_amount, Decimal("0"))

        duplicate = record_affiliate_signup(
            referral_code="AMINA123",
            email="NEWISP@example.com",
            company_name="Duplicate attempt",
        )
        self.assertEqual(duplicate.pk, referral.pk)
        self.assertEqual(AffiliateReferral.objects.count(), 1)


class AffiliateSuperadminTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.affiliate_user = User.objects.create_user(
            email="partner@example.com",
            password="Strong-Test-Pass-349!",
            phone_number="+254700001002",
        )
        self.account = AffiliateAccount.objects.create(
            user=self.affiliate_user,
            country="Kenya",
            currency="KES",
        )
        self.referral = AffiliateReferral.objects.create(
            affiliate=self.account,
            signup_email="isp@example.com",
            company_name="ISP Ltd",
            currency="KES",
        )
        self.superadmin = User.objects.create_superuser(
            email="root@example.com",
            password="Strong-Test-Pass-349!",
            phone_number="+254700001003",
        )

    def test_regular_affiliate_cannot_use_superadmin_controls(self):
        self.client.force_authenticate(self.affiliate_user)
        response = self.client.patch(
            f"/api/v1/affiliate/admin/referrals/{self.referral.id}/",
            {"reward_amount": "2500.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.referral.refresh_from_db()
        self.assertEqual(self.referral.reward_amount, Decimal("0"))

    def test_superadmin_manually_sets_commission_and_records_payout(self):
        self.client.force_authenticate(self.superadmin)
        response = self.client.patch(
            f"/api/v1/affiliate/admin/referrals/{self.referral.id}/",
            {"reward_amount": "2500.00", "status": "paid", "admin_notes": "Approved manually"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.referral.refresh_from_db()
        self.assertEqual(self.referral.reward_amount, Decimal("2500.00"))

        response = self.client.post(
            f"/api/v1/affiliate/admin/affiliates/{self.account.id}/payouts/",
            {
                "amount": "2500.00",
                "currency": "KES",
                "method": "mpesa",
                "status": "completed",
                "reference": "MANUAL-001",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        payout = AffiliatePayout.objects.get()
        self.assertEqual(payout.created_by, self.superadmin)
        self.assertIsNotNone(payout.processed_at)
