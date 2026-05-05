from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from datetime import timedelta
import uuid

from apps.core.models import User
from apps.core.otp_service import OTPService, OTPRateLimitedError, OTPError


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class OTPServiceTests(TestCase):
    def setUp(self):
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
