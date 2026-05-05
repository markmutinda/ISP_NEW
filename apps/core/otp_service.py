from datetime import timedelta
import secrets

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

from apps.core.models import EmailOTP


class OTPError(Exception):
    """Domain error for OTP workflows."""


class OTPRateLimitedError(OTPError):
    """Raised when OTP resend/request happens before cooldown."""


class OTPService:
    LOGIN_PURPOSE = "login"
    PAYMENT_PURPOSE = "payment_method_change"

    @staticmethod
    def _generate_code() -> str:
        return "".join(str(secrets.randbelow(10)) for _ in range(6))

    @classmethod
    def issue_otp(cls, user, purpose: str, ip_address: str = "") -> EmailOTP:
        cooldown_seconds = int(getattr(settings, "OTP_RESEND_COOLDOWN_SECONDS", 60))
        now = timezone.now()

        last = (
            EmailOTP.objects.filter(user=user, purpose=purpose)
            .order_by("-created_at")
            .first()
        )
        if last and (now - last.created_at).total_seconds() < cooldown_seconds:
            retry_after = cooldown_seconds - int((now - last.created_at).total_seconds())
            raise OTPRateLimitedError(f"Please wait {max(retry_after, 1)} seconds before requesting another OTP.")

        code = cls._generate_code()
        ttl_minutes = int(getattr(settings, "OTP_EXPIRY_MINUTES", 10))
        expires_at = now + timedelta(minutes=ttl_minutes)

        otp = EmailOTP.objects.create(
            user=user,
            purpose=purpose,
            code=code,
            expires_at=expires_at,
            ip_address=ip_address or "",
        )
        cls.send_otp_email(user=user, otp=otp)
        return otp

    @staticmethod
    def send_otp_email(user, otp: EmailOTP):
        context = {
            "user": user,
            "otp_code": otp.code,
            "expires_minutes": int(getattr(settings, "OTP_EXPIRY_MINUTES", 10)),
            "purpose": otp.get_purpose_display(),
            "company_name": getattr(user, "company_name", "Netily") or "Netily",
        }
        html_message = render_to_string("emails/otp_verification.html", context)
        plain_message = strip_tags(html_message)
        send_mail(
            subject="Your Netily verification code",
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            html_message=html_message,
            fail_silently=False,
        )

    @classmethod
    def verify_and_consume(cls, *, user, otp_id: str, code: str, purpose: str) -> EmailOTP:
        max_attempts = int(getattr(settings, "OTP_MAX_ATTEMPTS", 5))

        try:
            otp = EmailOTP.objects.get(id=otp_id, user=user, purpose=purpose)
        except EmailOTP.DoesNotExist as exc:
            raise OTPError("Invalid OTP session. Please request a new code.") from exc

        if otp.is_used:
            raise OTPError("This OTP has already been used.")

        if timezone.now() >= otp.expires_at:
            raise OTPError("OTP has expired. Please request a new one.")

        if otp.failed_attempts >= max_attempts:
            raise OTPError("Maximum verification attempts reached. Request a new OTP.")

        if otp.code != (code or "").strip():
            otp.failed_attempts += 1
            if otp.failed_attempts >= max_attempts:
                otp.is_used = True
            otp.save(update_fields=["failed_attempts", "is_used", "updated_at"])
            if otp.failed_attempts >= max_attempts:
                raise OTPError("Maximum verification attempts reached. Request a new OTP.")
            raise OTPError("Invalid OTP code.")

        otp.is_used = True
        otp.verified_at = timezone.now()
        otp.save(update_fields=["is_used", "verified_at", "updated_at"])
        return otp
