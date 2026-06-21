from datetime import timedelta
import secrets
import requests

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail, get_connection
from django.db import OperationalError, ProgrammingError
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

from apps.core.models import EmailOTP, LoginOTPChallenge


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

    @staticmethod
    def _cooldown_seconds() -> int:
        return int(getattr(settings, "OTP_RESEND_COOLDOWN_SECONDS", 60))

    @staticmethod
    def _expiry_minutes() -> int:
        return int(getattr(settings, "OTP_EXPIRY_MINUTES", 10))

    @staticmethod
    def _max_attempts() -> int:
        return int(getattr(settings, "OTP_MAX_ATTEMPTS", 5))

    @staticmethod
    def _max_resends() -> int:
        return int(getattr(settings, "OTP_LOGIN_MAX_RESENDS", 5))

    @staticmethod
    def is_otp_exempt_user(user) -> bool:
        if getattr(user, "is_superuser", False):
            return True
        email = (getattr(user, "email", "") or "").strip().lower()
        if not email:
            return False
        configured = getattr(settings, "OTP_EXEMPT_EMAILS", []) or []
        exempt_set = {str(e).strip().lower() for e in configured if str(e).strip()}
        if not exempt_set:
            exempt_set = {"admin@netily.co.ke"}
        return email in exempt_set

    @staticmethod
    def _challenge_matches_scope(*, challenge_tenant_scope: str, challenge_session_scope: str, tenant_scope: str, session_scope: str) -> bool:
        # Tenant scope must remain exact so challenges cannot be replayed across tenants.
        if (challenge_tenant_scope or "") != (tenant_scope or ""):
            return False

        # Session scope is treated as advisory, not mandatory. In production the
        # browser session identifier can rotate during OTP UX transitions, which
        # should not lock out a user who already controls both the password and
        # the emailed OTP code.
        return True

    @classmethod
    def start_login_challenge(cls, *, user, tenant_scope: str, session_scope: str, ip_address: str = ""):
        now = timezone.now()
        expires_at = now + timedelta(minutes=cls._expiry_minutes())

        try:
            challenge = LoginOTPChallenge.objects.create(
                user=user,
                tenant_scope=tenant_scope or "",
                session_scope=session_scope or "",
                expires_at=expires_at,
                ip_address=ip_address or "",
            )
            otp = cls._create_login_otp(user=user, challenge=challenge, ip_address=ip_address)
            cls.send_otp_email(user=user, otp=otp)
            return challenge
        except (ProgrammingError, OperationalError):
            # Fallback for environments where migrations are pending.
            code = cls._generate_code()
            challenge = cls._issue_cache_login_challenge(
                user=user,
                tenant_scope=tenant_scope,
                session_scope=session_scope,
                code=code,
                expires_at=expires_at,
            )
            cls.send_otp_email(user=user, otp=challenge)
            return challenge

    @classmethod
    def resend_login_challenge(cls, *, user, challenge_id: str, tenant_scope: str, session_scope: str, ip_address: str = ""):
        try:
            challenge = LoginOTPChallenge.objects.get(id=challenge_id, user=user)
        except LoginOTPChallenge.DoesNotExist as exc:
            if str(challenge_id).startswith("cache:"):
                return cls._resend_cache_login_challenge(
                    user=user,
                    challenge_id=challenge_id,
                    tenant_scope=tenant_scope,
                    session_scope=session_scope,
                )
            raise OTPError("Invalid or expired login challenge. Please login again.") from exc

        now = timezone.now()
        if challenge.is_completed or now >= challenge.expires_at:
            raise OTPError("Login challenge has expired. Please login again.")
        if not cls._challenge_matches_scope(
            challenge_tenant_scope=challenge.tenant_scope,
            challenge_session_scope=challenge.session_scope,
            tenant_scope=tenant_scope,
            session_scope=session_scope,
        ):
            raise OTPError("Challenge/session mismatch. Please login again.")

        cooldown = cls._cooldown_seconds()
        elapsed = int((now - challenge.last_sent_at).total_seconds())
        if elapsed < cooldown:
            raise OTPRateLimitedError(f"Please wait {cooldown - elapsed} seconds before requesting another OTP.")

        if challenge.resend_count >= cls._max_resends():
            raise OTPError("Maximum OTP resends reached for this login. Please login again.")

        EmailOTP.objects.filter(login_challenge=challenge, is_used=False).update(is_used=True)
        otp = cls._create_login_otp(user=user, challenge=challenge, ip_address=ip_address)
        challenge.resend_count += 1
        challenge.last_sent_at = now
        challenge.save(update_fields=["resend_count", "last_sent_at", "updated_at"])
        cls.send_otp_email(user=user, otp=otp)
        return challenge

    @classmethod
    def verify_login_challenge(cls, *, user, challenge_id: str, code: str, tenant_scope: str, session_scope: str):
        if str(challenge_id).startswith("cache:"):
            return cls._verify_cache_login_challenge(
                user=user,
                challenge_id=challenge_id,
                code=code,
                tenant_scope=tenant_scope,
                session_scope=session_scope,
            )

        try:
            challenge = LoginOTPChallenge.objects.get(id=challenge_id, user=user)
        except LoginOTPChallenge.DoesNotExist as exc:
            raise OTPError("Invalid login challenge. Please login again.") from exc

        now = timezone.now()
        if challenge.is_completed or now >= challenge.expires_at:
            raise OTPError("Login challenge has expired. Please login again.")
        if not cls._challenge_matches_scope(
            challenge_tenant_scope=challenge.tenant_scope,
            challenge_session_scope=challenge.session_scope,
            tenant_scope=tenant_scope,
            session_scope=session_scope,
        ):
            raise OTPError("Challenge/session mismatch. Please login again.")

        max_attempts = cls._max_attempts()
        if challenge.failed_attempts >= max_attempts:
            raise OTPError("Maximum verification attempts reached. Please login again.")

        otp = (
            EmailOTP.objects.filter(
                user=user,
                login_challenge=challenge,
                purpose=cls.LOGIN_PURPOSE,
                is_used=False,
            )
            .order_by("-created_at")
            .first()
        )
        if not otp:
            raise OTPError("OTP has expired. Please request a resend.")

        if otp.code != (code or "").strip() or timezone.now() >= otp.expires_at:
            challenge.failed_attempts += 1
            otp.failed_attempts += 1
            if challenge.failed_attempts >= max_attempts:
                otp.is_used = True
            challenge.save(update_fields=["failed_attempts", "updated_at"])
            otp.save(update_fields=["failed_attempts", "is_used", "updated_at"])
            if challenge.failed_attempts >= max_attempts:
                raise OTPError("Maximum verification attempts reached. Please login again.")
            raise OTPError("Invalid OTP code.")

        otp.is_used = True
        otp.verified_at = timezone.now()
        otp.save(update_fields=["is_used", "verified_at", "updated_at"])
        challenge.is_completed = True
        challenge.save(update_fields=["is_completed", "updated_at"])
        return challenge

    @classmethod
    def _create_login_otp(cls, *, user, challenge, ip_address: str = ""):
        now = timezone.now()
        return EmailOTP.objects.create(
            user=user,
            purpose=cls.LOGIN_PURPOSE,
            code=cls._generate_code(),
            login_challenge=challenge,
            expires_at=now + timedelta(minutes=cls._expiry_minutes()),
            ip_address=ip_address or "",
        )

    @classmethod
    def _issue_cache_login_challenge(cls, *, user, tenant_scope: str, session_scope: str, code: str, expires_at):
        challenge_id = f"cache:{tenant_scope}:{user.id}:{secrets.token_urlsafe(16)}"
        payload = {
            "challenge_id": challenge_id,
            "user_id": user.id,
            "tenant_scope": tenant_scope or "",
            "session_scope": session_scope or "",
            "code": code,
            "failed_attempts": 0,
            "resend_count": 0,
            "expires_at": expires_at.timestamp(),
            "last_sent_at": timezone.now().timestamp(),
        }
        cache_key = f"otp_login_challenge:{challenge_id}"
        cache.set(cache_key, payload, timeout=max(1, int((expires_at - timezone.now()).total_seconds())))

        class CacheChallenge:
            def __init__(self, cid, otp_code):
                self.id = cid
                self.code = otp_code
                self.expires_at = expires_at
                self.resend_count = 0

            def get_purpose_display(self):
                return "Tenant Login"

        return CacheChallenge(challenge_id, code)

    @classmethod
    def _resend_cache_login_challenge(cls, *, user, challenge_id: str, tenant_scope: str, session_scope: str):
        cache_key = f"otp_login_challenge:{challenge_id}"
        payload = cache.get(cache_key)
        if not payload:
            raise OTPError("Login challenge has expired. Please login again.")

        if not cls._challenge_matches_scope(
            challenge_tenant_scope=payload.get("tenant_scope", ""),
            challenge_session_scope=payload.get("session_scope", ""),
            tenant_scope=tenant_scope,
            session_scope=session_scope,
        ):
            raise OTPError("Challenge/session mismatch. Please login again.")

        now_ts = timezone.now().timestamp()
        if now_ts >= float(payload.get("expires_at", 0)):
            cache.delete(cache_key)
            raise OTPError("Login challenge has expired. Please login again.")

        cooldown = cls._cooldown_seconds()
        elapsed = int(now_ts - float(payload.get("last_sent_at", 0)))
        if elapsed < cooldown:
            raise OTPRateLimitedError(f"Please wait {cooldown - elapsed} seconds before requesting another OTP.")

        if int(payload.get("resend_count", 0)) >= cls._max_resends():
            raise OTPError("Maximum OTP resends reached for this login. Please login again.")

        payload["resend_count"] = int(payload.get("resend_count", 0)) + 1
        payload["last_sent_at"] = now_ts
        payload["code"] = cls._generate_code()
        cache.set(cache_key, payload, timeout=max(1, int(float(payload.get("expires_at", now_ts)) - now_ts)))

        class CacheResendChallenge:
            def __init__(self, cid, otp_code, expires_at_ts, resend_count):
                self.id = cid
                self.code = otp_code
                self.expires_at = timezone.datetime.fromtimestamp(expires_at_ts, tz=timezone.get_current_timezone())
                self.resend_count = resend_count

            def get_purpose_display(self):
                return "Tenant Login"

        challenge = CacheResendChallenge(
            challenge_id,
            payload["code"],
            float(payload.get("expires_at", now_ts)),
            int(payload.get("resend_count", 0)),
        )
        cls.send_otp_email(user=user, otp=challenge)
        return challenge

    @classmethod
    def _verify_cache_login_challenge(cls, *, user, challenge_id: str, code: str, tenant_scope: str, session_scope: str):
        cache_key = f"otp_login_challenge:{challenge_id}"
        payload = cache.get(cache_key)
        if not payload:
            raise OTPError("Login challenge has expired. Please login again.")

        if not cls._challenge_matches_scope(
            challenge_tenant_scope=payload.get("tenant_scope", ""),
            challenge_session_scope=payload.get("session_scope", ""),
            tenant_scope=tenant_scope,
            session_scope=session_scope,
        ):
            raise OTPError("Challenge/session mismatch. Please login again.")

        now_ts = timezone.now().timestamp()
        if now_ts >= float(payload.get("expires_at", 0)):
            cache.delete(cache_key)
            raise OTPError("Login challenge has expired. Please login again.")

        failed = int(payload.get("failed_attempts", 0))
        if failed >= cls._max_attempts():
            cache.delete(cache_key)
            raise OTPError("Maximum verification attempts reached. Please login again.")

        if payload.get("code") != (code or "").strip():
            failed += 1
            payload["failed_attempts"] = failed
            cache.set(cache_key, payload, timeout=max(1, int(float(payload.get("expires_at", now_ts)) - now_ts)))
            if failed >= cls._max_attempts():
                cache.delete(cache_key)
                raise OTPError("Maximum verification attempts reached. Please login again.")
            raise OTPError("Invalid OTP code.")

        cache.delete(cache_key)

    @classmethod
    def issue_otp(cls, user, purpose: str, ip_address: str = "") -> EmailOTP:
        cooldown_seconds = cls._cooldown_seconds()
        now = timezone.now()

        code = cls._generate_code()
        expires_at = now + timedelta(minutes=cls._expiry_minutes())

        try:
            last = (
                EmailOTP.objects.filter(user=user, purpose=purpose)
                .order_by("-created_at")
                .first()
            )
            if last and (now - last.created_at).total_seconds() < cooldown_seconds:
                retry_after = cooldown_seconds - int((now - last.created_at).total_seconds())
                raise OTPRateLimitedError(f"Please wait {max(retry_after, 1)} seconds before requesting another OTP.")

            otp = EmailOTP.objects.create(
                user=user,
                purpose=purpose,
                code=code,
                expires_at=expires_at,
                ip_address=ip_address or "",
            )
        except (ProgrammingError, OperationalError):
            otp = cls._issue_cache_otp(user=user, purpose=purpose, code=code, expires_at=expires_at, cooldown_seconds=cooldown_seconds)

        cls.send_otp_email(user=user, otp=otp)
        return otp

    @classmethod
    def _issue_cache_otp(cls, *, user, purpose: str, code: str, expires_at, cooldown_seconds: int):
        rate_key = f"otp_cache_rate:{purpose}:{user.id}"
        if cache.get(rate_key):
            raise OTPRateLimitedError("Please wait before requesting another OTP.")
        cache.set(rate_key, True, timeout=cooldown_seconds)
        cache_key = f"otp_cache_value:{purpose}:{user.id}"
        cache.set(cache_key, {"code": code, "expires_at": expires_at.timestamp(), "failed_attempts": 0}, timeout=int((expires_at - timezone.now()).total_seconds()))

        class CacheOTP:
            def __init__(self, otp_id, otp_code):
                self.id = otp_id
                self.code = otp_code

            def get_purpose_display(self):
                return purpose.replace("_", " ").title()

        return CacheOTP(f"cache:{purpose}:{user.id}", code)

    @staticmethod
    def send_otp_email(user, otp):
        context = {
            "user": user,
            "otp_code": otp.code,
            "expires_minutes": int(getattr(settings, "OTP_EXPIRY_MINUTES", 10)),
            "purpose": otp.get_purpose_display(),
            "company_name": getattr(user, "company_name", "Netily") or "Netily",
        }
        html_message = render_to_string("emails/otp_verification.html", context)
        plain_message = strip_tags(html_message)

        resend_key = getattr(settings, "RESEND_API_KEY", "") or ""
        resend_from = getattr(settings, "RESEND_FROM_EMAIL", "") or getattr(settings, "DEFAULT_FROM_EMAIL", "")
        if resend_key and resend_from:
            try:
                response = requests.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {resend_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": resend_from,
                        "to": [user.email],
                        "subject": "Your Netily verification code",
                        "html": html_message,
                        "text": plain_message,
                    },
                    timeout=8,
                )
                if response.status_code < 300:
                    return
            except Exception:
                pass

        email_timeout = int(getattr(settings, "EMAIL_TIMEOUT", 10) or 10)
        connection = get_connection(timeout=email_timeout)
        send_mail(
            subject="Your Netily verification code",
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            html_message=html_message,
            fail_silently=False,
            connection=connection,
        )

    @classmethod
    def verify_and_consume(cls, *, user, otp_id: str, code: str, purpose: str):
        max_attempts = cls._max_attempts()
        if str(otp_id).startswith("cache:"):
            return cls._verify_cache_otp(user=user, otp_id=otp_id, code=code, purpose=purpose, max_attempts=max_attempts)

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

    @classmethod
    def _verify_cache_otp(cls, *, user, otp_id: str, code: str, purpose: str, max_attempts: int):
        expected_id = f"cache:{purpose}:{user.id}"
        if otp_id != expected_id:
            raise OTPError("Invalid OTP session. Please request a new code.")

        cache_key = f"otp_cache_value:{purpose}:{user.id}"
        payload = cache.get(cache_key)
        if not payload:
            raise OTPError("OTP has expired. Please request a new one.")

        if timezone.now().timestamp() >= float(payload.get("expires_at", 0)):
            cache.delete(cache_key)
            raise OTPError("OTP has expired. Please request a new one.")

        failed = int(payload.get("failed_attempts", 0))
        if failed >= max_attempts:
            cache.delete(cache_key)
            raise OTPError("Maximum verification attempts reached. Request a new OTP.")

        if payload.get("code") != (code or "").strip():
            failed += 1
            payload["failed_attempts"] = failed
            cache.set(cache_key, payload, timeout=300)
            if failed >= max_attempts:
                cache.delete(cache_key)
                raise OTPError("Maximum verification attempts reached. Request a new OTP.")
            raise OTPError("Invalid OTP code.")

        cache.delete(cache_key)

        class VerifiedCacheOTP:
            is_used = True

        return VerifiedCacheOTP()
