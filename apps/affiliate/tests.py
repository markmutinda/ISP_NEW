from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.models import EmailOTP, GlobalSystemSettings, Lead, LoginOTPChallenge, User

from .models import AffiliateAccount, AffiliateClick, AffiliatePayout, AffiliateReferral
from .services import record_affiliate_signup
from .views import _send_verification


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


class AffiliateVerificationEmailTests(SimpleTestCase):
    @patch("apps.affiliate.views.send_transactional_email")
    def test_verification_uses_transactional_delivery_and_returns_feedback(self, delivery):
        delivery.return_value = {"sent": True, "provider": "smtp"}
        account = SimpleNamespace(
            id=42,
            user=SimpleNamespace(email="affiliate@example.com"),
        )

        result = _send_verification(account)

        self.assertTrue(result["sent"])
        delivery.assert_called_once()
        call = delivery.call_args.kwargs
        self.assertEqual(call["recipient"], "affiliate@example.com")
        self.assertIn("/affiliate/verify?token=", call["plain_message"])
        self.assertIn("/affiliate/verify?token=", call["html_message"])


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

    def test_affiliate_login_otp_is_disabled_by_default(self):
        response = self.client.post(
            "/api/v1/affiliate/login/",
            {"email": self.user.email, "password": "Strong-Test-Pass-349!"},
            format="json",
            HTTP_X_SESSION_ID="affiliate-browser-default",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertFalse(GlobalSystemSettings.get_solo().affiliate_email_otp_enabled)

    def test_active_affiliate_can_use_enabled_otp_and_read_own_profile(self):
        settings_obj = GlobalSystemSettings.get_solo()
        settings_obj.affiliate_email_otp_enabled = True
        settings_obj.save(update_fields=["affiliate_email_otp_enabled"])
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
        self.assertTrue(response.data["referral_link"].endswith("/affiliate/AMINA123"))

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
        replay = self.client.post(
            "/api/v1/affiliate/r/AMINA123/click/",
            {"source": "WhatsApp", "attribution_token": str(click.attribution_token)},
            format="json",
        )
        self.assertEqual(replay.status_code, 200)
        self.assertFalse(replay.data["recorded"])
        self.assertEqual(AffiliateClick.objects.filter(affiliate=self.account).count(), 1)
        recent_duplicate = self.client.post(
            "/api/v1/affiliate/r/AMINA123/click/",
            {"source": "Direct"},
            format="json",
        )
        self.assertEqual(recent_duplicate.status_code, 200)
        self.assertFalse(recent_duplicate.data["recorded"])
        self.assertEqual(recent_duplicate.data["attribution_token"], str(click.attribution_token))
        self.assertEqual(AffiliateClick.objects.filter(affiliate=self.account).count(), 1)

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
        self.assertIsNone(duplicate)
        self.assertEqual(AffiliateReferral.objects.count(), 1)

    def test_referral_code_without_matching_click_token_is_not_attributed(self):
        referral = record_affiliate_signup(
            referral_code="AMINA123",
            email="untracked@example.com",
            company_name="Untracked ISP",
        )
        self.assertIsNone(referral)
        self.assertFalse(AffiliateReferral.objects.filter(signup_email="untracked@example.com").exists())

        self_referral = record_affiliate_signup(
            referral_code="AMINA123",
            attribution_token=str(
                AffiliateClick.objects.create(
                    affiliate=self.account,
                    attribution_token="72a46f35-30bf-44cd-a353-59fa0f4bd08b",
                ).attribution_token
            ),
            email=self.user.email,
        )
        self.assertIsNone(self_referral)

    def test_click_token_cannot_be_replayed_for_multiple_signup_emails(self):
        click = AffiliateClick.objects.create(
            affiliate=self.account,
            attribution_token="2c6e5ae5-5c88-4e88-9697-982876c7a9da",
        )
        first = record_affiliate_signup(
            referral_code="AMINA123",
            attribution_token=str(click.attribution_token),
            email="first-isp@example.com",
        )
        replay = record_affiliate_signup(
            referral_code="AMINA123",
            attribution_token=str(click.attribution_token),
            email="second-isp@example.com",
        )

        self.assertIsNotNone(first)
        self.assertIsNone(replay)
        self.assertEqual(click.signups.count(), 1)

    def test_tracked_lead_is_structured_as_an_affiliate_referral(self):
        click_response = self.client.post(
            "/api/v1/affiliate/r/AMINA123/click/",
            {"source": "LinkedIn"},
            format="json",
        )
        self.assertEqual(click_response.status_code, 200)

        response = self.client.post(
            "/api/v1/core/leads/submit/",
            {
                "name": "Referred ISP",
                "email": "referred-isp@example.com",
                "company": "Referred Networks",
                "lead_source": "Other",
                "referral_code": "AMINA123",
                "attribution_token": click_response.data["attribution_token"],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        lead = Lead.objects.get(email="referred-isp@example.com")
        referral = AffiliateReferral.objects.get(lead=lead)
        self.assertEqual(lead.lead_source, "Affiliate Referral")
        self.assertEqual(referral.affiliate, self.account)
        self.assertEqual(referral.status, "pending")
        self.assertEqual(referral.reward_amount, Decimal("0"))


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

    def test_superadmin_controls_affiliate_otp_and_uses_one_time_account_access(self):
        self.client.force_authenticate(self.superadmin)
        response = self.client.patch(
            "/api/v1/affiliate/admin/settings/",
            {"affiliate_email_otp_enabled": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["affiliate_email_otp_enabled"])

        response = self.client.post(f"/api/v1/affiliate/admin/affiliates/{self.account.id}/access/")
        self.assertEqual(response.status_code, 200)
        token = parse_qs(urlparse(response.data["access_url"]).query)["token"][0]

        self.client.force_authenticate(user=None)
        exchange = self.client.post(
            "/api/v1/affiliate/admin-access/exchange/",
            {"token": token},
            format="json",
        )
        self.assertEqual(exchange.status_code, 200)
        self.assertIn("access", exchange.data)
        self.assertEqual(exchange.data["user"]["id"], self.account.id)

        replay = self.client.post(
            "/api/v1/affiliate/admin-access/exchange/",
            {"token": token},
            format="json",
        )
        self.assertEqual(replay.status_code, 400)

    def test_superadmin_can_create_update_and_soft_delete_affiliate(self):
        self.client.force_authenticate(self.superadmin)
        created = self.client.post(
            "/api/v1/affiliate/admin/affiliates/",
            {
                "full_name": "Manual Partner",
                "email": "manual-partner@example.com",
                "phone": "+254700001099",
                "country": "Kenya",
                "password": "Strong-Manual-Pass-721!",
                "is_verified": True,
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201)
        affiliate_id = created.data["id"]

        updated = self.client.patch(
            f"/api/v1/affiliate/admin/affiliates/{affiliate_id}/",
            {"full_name": "Updated Partner", "tier": "silver", "status": "suspended"},
            format="json",
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data["full_name"], "Updated Partner")
        self.assertEqual(updated.data["tier"], "silver")

        deleted = self.client.delete(f"/api/v1/affiliate/admin/affiliates/{affiliate_id}/")
        self.assertEqual(deleted.status_code, 204)
        account = AffiliateAccount.objects.get(pk=affiliate_id)
        account.user.refresh_from_db()
        self.assertEqual(account.status, "inactive")
        self.assertFalse(account.user.is_active)

    def test_completed_payout_cannot_exceed_manually_approved_commission(self):
        self.referral.status = "approved"
        self.referral.reward_amount = Decimal("1000.00")
        self.referral.save(update_fields=["status", "reward_amount"])
        self.client.force_authenticate(self.superadmin)

        response = self.client.post(
            f"/api/v1/affiliate/admin/affiliates/{self.account.id}/payouts/",
            {
                "amount": "1001.00",
                "currency": "KES",
                "method": "mpesa",
                "status": "completed",
                "reference": "MANUAL-OVERPAY",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AffiliatePayout.objects.exists())
