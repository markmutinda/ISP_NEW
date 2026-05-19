from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from datetime import timedelta
import uuid

from apps.core.models import User
from apps.core.email_delivery import send_transactional_email
from apps.core.otp_service import OTPService, OTPRateLimitedError, OTPError


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class OTPServiceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.filter(email__isnull=False).first()
        if not self.user:
            suffix = uuid.uuid4().hex[:8]
            self.user = User.objects.create_user(
                email=f"tenant.admin.{suffix}@example.com",
                password="StrongPass123!",
                first_name="Tenant",
                last_name="Admin",
                phone_number=f"+2547000{suffix[:5]}",
                role="admin",
            )

    def test_issue_and_verify_otp(self):
        otp = OTPService.issue_otp(self.user, OTPService.LOGIN_PURPOSE)
        self.assertEqual(len(mail.outbox), 1)
        verified = OTPService.verify_and_consume(
            user=self.user,
            otp_id=otp.id,
            code=otp.code,
            purpose=OTPService.LOGIN_PURPOSE,
        )
        self.assertTrue(verified.is_used)

    def test_rate_limit_is_enforced(self):
        OTPService.issue_otp(self.user, OTPService.PAYMENT_PURPOSE)
        with self.assertRaises(OTPRateLimitedError):
            OTPService.issue_otp(self.user, OTPService.PAYMENT_PURPOSE)

    def test_max_attempts_invalidate_otp(self):
        otp = OTPService.issue_otp(self.user, OTPService.PAYMENT_PURPOSE)
        for _ in range(5):
            with self.assertRaises(OTPError):
                OTPService.verify_and_consume(
                    user=self.user,
                    otp_id=otp.id,
                    code="000000",
                    purpose=OTPService.PAYMENT_PURPOSE,
                )
        otp.refresh_from_db()
        self.assertTrue(otp.is_used)

    def test_expired_otp_rejected(self):
        otp = OTPService.issue_otp(self.user, OTPService.LOGIN_PURPOSE)
        otp.expires_at = timezone.now() - timedelta(seconds=1)
        otp.save(update_fields=["expires_at"])

        with self.assertRaises(OTPError):
            OTPService.verify_and_consume(
                user=self.user,
                otp_id=otp.id,
                code=otp.code,
                purpose=OTPService.LOGIN_PURPOSE,
            )

    def test_login_challenge_resend_and_verify(self):
        challenge = OTPService.start_login_challenge(
            user=self.user,
            tenant_scope="tenant_demo",
            session_scope="browser-a",
            ip_address="127.0.0.1",
        )
        self.assertIsNotNone(challenge.id)

        with self.assertRaises(OTPRateLimitedError):
            OTPService.resend_login_challenge(
                user=self.user,
                challenge_id=str(challenge.id),
                tenant_scope="tenant_demo",
                session_scope="browser-a",
                ip_address="127.0.0.1",
            )

        # move cooldown back
        challenge.last_sent_at = timezone.now() - timedelta(seconds=61)
        challenge.save(update_fields=["last_sent_at"])
        challenge = OTPService.resend_login_challenge(
            user=self.user,
            challenge_id=str(challenge.id),
            tenant_scope="tenant_demo",
            session_scope="browser-a",
            ip_address="127.0.0.1",
        )
        latest_otp = challenge.otps.order_by("-created_at").first()
        OTPService.verify_login_challenge(
            user=self.user,
            challenge_id=str(challenge.id),
            code=latest_otp.code,
            tenant_scope="tenant_demo",
            session_scope="browser-a",
        )


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RESEND_API_KEY="",
)
class TransactionalEmailTests(TestCase):
    def test_smtp_fallback_returns_delivery_feedback(self):
        result = send_transactional_email(
            subject="Welcome",
            recipient="tenant@example.com",
            plain_message="Plain body",
            html_message="<p>HTML body</p>",
            from_email="Netily <noreply@example.com>",
        )

        self.assertTrue(result["sent"])
        self.assertEqual(result["provider"], "smtp")
        self.assertEqual(len(mail.outbox), 1)
