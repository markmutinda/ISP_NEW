import csv
import hashlib
import ipaddress
import io
import logging
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core import signing
from django.core.cache import cache
from django.db import connection
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import AccessToken, Token

from apps.superadmin.permissions import IsSuperAdmin
from apps.superadmin.models import SuperAdminActivityLog
from apps.core.otp_service import OTPError, OTPRateLimitedError, OTPService
from apps.core.email_delivery import send_transactional_email
from apps.core.models import EmailOTP, GlobalSystemSettings, User

from .models import AffiliateAccount, AffiliateClick, AffiliatePayout, AffiliateReferral
from .permissions import IsActiveAffiliate
from .serializers import (
    AffiliateAccountAdminSerializer,
    AffiliateAccountAdminCreateSerializer,
    AffiliatePayoutAdminSerializer,
    AffiliateReferralAdminCreateSerializer,
    AffiliateReferralAdminSerializer,
    AffiliateRegisterSerializer,
    affiliate_user_data,
    payout_data,
    referral_data,
)

AFFILIATE_PERMS = [IsAuthenticated, IsActiveAffiliate]
SUPERADMIN_PERMS = [IsAuthenticated, IsSuperAdmin]
VERIFICATION_SALT = "affiliate-email-verification"
ADMIN_ACCESS_SALT = "affiliate-admin-access"
AFFILIATE_TEMP_PASSWORD_CACHE_PREFIX = "affiliate_temp_password"
logger = logging.getLogger(__name__)


class AffiliateRefreshToken(Token):
    """Public-schema refresh token without the tenant-only blacklist tables."""

    token_type = "refresh"
    lifetime = api_settings.REFRESH_TOKEN_LIFETIME
    access_token_class = AccessToken
    no_copy_claims = (api_settings.TOKEN_TYPE_CLAIM, "exp", api_settings.JTI_CLAIM, "jti", "iat")

    @property
    def access_token(self):
        access = self.access_token_class()
        access.set_exp(from_time=self.current_time)
        for claim, value in self.payload.items():
            if claim not in self.no_copy_claims:
                access[claim] = value
        return access


def _ensure_public():
    if hasattr(connection, "set_schema_to_public"):
        connection.set_schema_to_public()


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    value = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return ""


def _audit_admin(request, action, summary, account, metadata=None):
    SuperAdminActivityLog.objects.create(
        actor=request.user,
        target_user=account.user,
        action=action,
        summary=summary[:255],
        metadata=metadata or {},
        ip_address=_client_ip(request) or None,
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
    )


def _send_verification(account):
    token = signing.dumps({"affiliate_id": account.id, "email": account.user.email}, salt=VERIFICATION_SALT)
    frontend = getattr(settings, "FRONTEND_URL", "https://netily.co.ke").rstrip("/")
    url = f"{frontend}/affiliate/verify?token={token}"
    plain_message = (
        "Welcome to the Netily Affiliate Program.\n\n"
        f"Verify your email by opening this link:\n{url}\n\n"
        "This link expires in 24 hours. If you did not create this account, you can ignore this email."
    )
    html_message = (
        "<h2>Verify your Netily affiliate account</h2>"
        "<p>Welcome to the Netily Affiliate Program.</p>"
        f'<p><a href="{url}">Verify my email address</a></p>'
        "<p>This link expires in 24 hours. If you did not create this account, you can ignore this email.</p>"
    )
    result = send_transactional_email(
        subject="Verify your Netily affiliate account",
        recipient=account.user.email,
        plain_message=plain_message,
        html_message=html_message,
    )
    if not result.get("sent"):
        logger.error(
            "Affiliate verification delivery failed account_id=%s provider=%s error=%s",
            account.id,
            result.get("provider"),
            result.get("error", "unknown"),
        )
    return result


def _send_affiliate_password_email(*, account, subject: str, plain_message: str, html_message: str):
    result = send_transactional_email(
        subject=subject,
        recipient=account.user.email,
        plain_message=plain_message,
        html_message=html_message,
    )
    if not result.get("sent"):
        logger.error(
            "Affiliate password email failed account_id=%s provider=%s error=%s",
            account.id,
            result.get("provider"),
            result.get("error", "unknown"),
        )
    return result


def _generate_temporary_password(length=14):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    special = "!@#$%?"
    raw = [
        secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ"),
        secrets.choice("abcdefghijkmnopqrstuvwxyz"),
        secrets.choice("23456789"),
        secrets.choice(special),
    ]
    raw.extend(secrets.choice(alphabet + special) for _ in range(max(length - 4, 8)))
    secrets.SystemRandom().shuffle(raw)
    return "".join(raw)


def _period_start(value):
    days = {"7d": 7, "30d": 30, "90d": 90}.get(value, 30)
    return timezone.now() - timedelta(days=days)


def _affiliate_session_scope(request):
    return (
        request.headers.get("X-Session-ID")
        or request.COOKIES.get("sessionid")
        or request.session.session_key
        or f"{_client_ip(request)}:{request.META.get('HTTP_USER_AGENT', '')[:40]}"
    )


def _masked_email(email):
    parts = (email or "").split("@")
    return f"{parts[0][:2]}***@{parts[1]}" if len(parts) == 2 else "***"


def _affiliate_login_account(request):
    email = (request.data.get("email") or "").strip().lower()
    password = request.data.get("password") or ""
    user = authenticate(request=request, username=email, password=password)
    if not user:
        user = authenticate(email=email, password=password)
    account = getattr(user, "affiliate_account", None) if user else None
    return user, account


def _affiliate_or_404(pk):
    return AffiliateAccount.objects.select_related("user").filter(pk=pk).first()


class AffiliateRegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = "affiliate_register"

    def post(self, request):
        _ensure_public()
        serializer = AffiliateRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = serializer.save()
        delivery = _send_verification(account)
        sent = bool(delivery.get("sent"))
        return Response(
            {
                "user": affiliate_user_data(account),
                "verification_email_sent": sent,
                "message": (
                    "Verification email sent."
                    if sent
                    else "Account created, but the verification email could not be delivered. Please use resend."
                ),
            },
            status=status.HTTP_201_CREATED,
        )


class AffiliateLoginView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = "affiliate_login"

    def post(self, request):
        _ensure_public()
        user, account = _affiliate_login_account(request)
        if not user or not user.is_active or not account:
            return Response({"detail": "Invalid affiliate credentials."}, status=status.HTTP_400_BAD_REQUEST)
        if account.status != "active":
            return Response({"detail": "This affiliate account is not active."}, status=status.HTTP_403_FORBIDDEN)
        if not account.is_verified:
            return Response({"detail": "Verify your email before signing in."}, status=status.HTTP_403_FORBIDDEN)

        challenge_id = request.data.get("challenge_id")
        otp_code = (request.data.get("otp_code") or "").strip()
        if bool(challenge_id) != bool(otp_code):
            return Response({"detail": "Both challenge_id and otp_code are required."}, status=400)

        affiliate_otp_enabled = bool(GlobalSystemSettings.get_solo().affiliate_email_otp_enabled)
        requires_otp = affiliate_otp_enabled and not OTPService.is_otp_exempt_user(user)
        tenant_scope = "public:affiliate"
        session_scope = _affiliate_session_scope(request)
        if requires_otp and not challenge_id:
            try:
                challenge = OTPService.start_login_challenge(
                    user=user,
                    tenant_scope=tenant_scope,
                    session_scope=session_scope,
                    ip_address=_client_ip(request),
                )
            except OTPRateLimitedError as exc:
                return Response({"detail": str(exc)}, status=429)
            except Exception:
                return Response({"detail": "Failed to send OTP email. Please try again."}, status=500)
            return Response({
                "requires_otp": True,
                "challenge_id": str(challenge.id),
                "email": _masked_email(user.email),
                "message": "OTP sent to your registered email.",
                "expires_in": int((challenge.expires_at - timezone.now()).total_seconds()),
                "resend_available_in": int(getattr(settings, "OTP_RESEND_COOLDOWN_SECONDS", 60)),
                "max_resends": int(getattr(settings, "OTP_LOGIN_MAX_RESENDS", 5)),
            }, status=202)

        if requires_otp:
            try:
                OTPService.verify_login_challenge(
                    user=user,
                    challenge_id=challenge_id,
                    code=otp_code,
                    tenant_scope=tenant_scope,
                    session_scope=session_scope,
                )
            except OTPError as exc:
                return Response({"detail": str(exc)}, status=400)

        refresh = AffiliateRefreshToken.for_user(user)
        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])
        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": affiliate_user_data(account),
        })


class AffiliateResendLoginOTPView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "affiliate_login"

    def post(self, request):
        _ensure_public()
        user, account = _affiliate_login_account(request)
        challenge_id = request.data.get("challenge_id")
        if not user or not user.is_active or not account:
            return Response({"detail": "Invalid affiliate credentials."}, status=400)
        if account.status != "active" or not account.is_verified:
            return Response({"detail": "This affiliate account cannot sign in."}, status=403)
        if not challenge_id:
            return Response({"detail": "challenge_id is required."}, status=400)
        try:
            challenge = OTPService.resend_login_challenge(
                user=user,
                challenge_id=challenge_id,
                tenant_scope="public:affiliate",
                session_scope=_affiliate_session_scope(request),
                ip_address=_client_ip(request),
            )
        except OTPRateLimitedError as exc:
            return Response({"detail": str(exc)}, status=429)
        except OTPError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({
            "requires_otp": True,
            "challenge_id": str(challenge.id),
            "email": _masked_email(user.email),
            "message": "A new OTP has been sent.",
            "expires_in": int((challenge.expires_at - timezone.now()).total_seconds()),
            "resend_available_in": int(getattr(settings, "OTP_RESEND_COOLDOWN_SECONDS", 60)),
            "resend_count": int(getattr(challenge, "resend_count", 0)),
            "max_resends": int(getattr(settings, "OTP_LOGIN_MAX_RESENDS", 5)),
        })


class AffiliateTokenRefreshView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        _ensure_public()
        try:
            refresh = AffiliateRefreshToken(request.data.get("refresh", ""))
            user_id = refresh[api_settings.USER_ID_CLAIM]
            account = AffiliateAccount.objects.select_related("user").get(
                user_id=user_id,
                status="active",
                user__is_active=True,
            )
        except Exception:
            return Response({"detail": "Refresh token is invalid or expired."}, status=401)
        access = refresh.access_token
        access[api_settings.USER_ID_CLAIM] = str(account.user_id)
        return Response({"access": str(access)})


class AffiliateMeView(APIView):
    permission_classes = AFFILIATE_PERMS

    def get(self, request):
        _ensure_public()
        return Response(affiliate_user_data(request.user.affiliate_account))


class AffiliateVerificationView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = "affiliate_verify"

    def post(self, request):
        _ensure_public()
        try:
            payload = signing.loads(
                request.data.get("token", ""),
                salt=VERIFICATION_SALT,
                max_age=86400,
            )
            account = AffiliateAccount.objects.select_related("user").get(
                pk=payload["affiliate_id"],
                user__email__iexact=payload["email"],
            )
        except (signing.BadSignature, signing.SignatureExpired, AffiliateAccount.DoesNotExist, KeyError):
            return Response({"detail": "This verification link is invalid or expired."}, status=400)
        if not account.is_verified:
            account.is_verified = True
            account.verified_at = timezone.now()
            account.save(update_fields=["is_verified", "verified_at", "updated_at"])
            account.user.is_verified = True
            account.user.save(update_fields=["is_verified"])
        return Response({"verified": True})


class AffiliateResendVerificationView(APIView):
    permission_classes = [AllowAny]
    throttle_scope = "affiliate_verify"

    def post(self, request):
        _ensure_public()
        email = (request.data.get("email") or "").strip().lower()
        account = AffiliateAccount.objects.select_related("user").filter(user__email__iexact=email).first()
        if account and not account.is_verified:
            delivery = _send_verification(account)
            if not delivery.get("sent"):
                return Response(
                    {"detail": "Verification email delivery is temporarily unavailable. Please try again shortly."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
        return Response({"detail": "If the account exists, a verification email has been sent."})


class AffiliatePasswordResetOTPRequestView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "affiliate_verify"

    def post(self, request):
        _ensure_public()
        email = (request.data.get("email") or "").strip().lower()
        account = AffiliateAccount.objects.select_related("user").filter(user__email__iexact=email).first()
        if not account or not account.user.is_active or account.status != "active":
            return Response({"detail": "If an active affiliate account exists, a reset code has been sent."})
        try:
            otp = OTPService.issue_otp(
                account.user,
                purpose=EmailOTP.PURPOSE_AFFILIATE_PASSWORD_RESET,
                ip_address=_client_ip(request),
            )
        except OTPRateLimitedError as exc:
            return Response({"detail": str(exc)}, status=429)
        except Exception:
            logger.exception("Affiliate password reset OTP failed account_id=%s", account.id)
            return Response({"detail": "Password reset email is temporarily unavailable. Please try again."}, status=503)
        return Response({
            "detail": "If an active affiliate account exists, a reset code has been sent.",
            "otp_id": str(otp.id),
            "email": _masked_email(account.user.email),
            "expires_in": int((otp.expires_at - timezone.now()).total_seconds()) if hasattr(otp, "expires_at") else int(getattr(settings, "OTP_EXPIRY_MINUTES", 10)) * 60,
            "resend_available_in": int(getattr(settings, "OTP_RESEND_COOLDOWN_SECONDS", 60)),
        })


class AffiliatePasswordResetOTPConfirmView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "affiliate_verify"

    def post(self, request):
        _ensure_public()
        email = (request.data.get("email") or "").strip().lower()
        otp_id = request.data.get("otp_id") or ""
        otp_code = (request.data.get("otp_code") or "").strip()
        new_password = request.data.get("new_password") or ""
        confirm_password = request.data.get("confirm_password") or ""
        if new_password != confirm_password:
            return Response({"detail": "Passwords do not match."}, status=400)
        account = AffiliateAccount.objects.select_related("user").filter(user__email__iexact=email).first()
        if not account or not account.user.is_active or account.status != "active":
            return Response({"detail": "Invalid reset session."}, status=400)
        try:
            validate_password(new_password, user=account.user)
            OTPService.verify_and_consume(
                user=account.user,
                otp_id=otp_id,
                code=otp_code,
                purpose=EmailOTP.PURPOSE_AFFILIATE_PASSWORD_RESET,
            )
        except ValidationError as exc:
            return Response({"detail": " ".join(exc.messages)}, status=400)
        except OTPError as exc:
            return Response({"detail": str(exc)}, status=400)
        account.user.set_password(new_password)
        account.user.save(update_fields=["password"])
        cache.delete(f"{AFFILIATE_TEMP_PASSWORD_CACHE_PREFIX}:{account.user_id}")
        return Response({"detail": "Password changed. You can now sign in with your new password."})


class AffiliateTemporaryPasswordConfirmView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "affiliate_login"

    def post(self, request):
        _ensure_public()
        email = (request.data.get("email") or "").strip().lower()
        temporary_password = request.data.get("temporary_password") or ""
        new_password = request.data.get("new_password") or ""
        confirm_password = request.data.get("confirm_password") or ""
        if new_password != confirm_password:
            return Response({"detail": "Passwords do not match."}, status=400)
        user = authenticate(request=request, username=email, password=temporary_password)
        if not user:
            user = authenticate(email=email, password=temporary_password)
        account = getattr(user, "affiliate_account", None) if user else None
        if not user or not account or account.status != "active" or not user.is_active:
            return Response({"detail": "Invalid temporary password."}, status=400)
        if not cache.get(f"{AFFILIATE_TEMP_PASSWORD_CACHE_PREFIX}:{user.id}"):
            return Response({"detail": "This account does not have an active temporary password reset."}, status=400)
        try:
            validate_password(new_password, user=user)
        except ValidationError as exc:
            return Response({"detail": " ".join(exc.messages)}, status=400)
        user.set_password(new_password)
        user.save(update_fields=["password"])
        cache.delete(f"{AFFILIATE_TEMP_PASSWORD_CACHE_PREFIX}:{user.id}")
        return Response({"detail": "Password changed. You can now sign in with your new password."})


class AffiliateClickView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "affiliate_click"

    def post(self, request, code):
        _ensure_public()
        account = AffiliateAccount.objects.filter(referral_code=code.upper(), status="active").first()
        if not account:
            return Response({"detail": "Referral link not found."}, status=404)

        existing_token = request.data.get("attribution_token")
        if existing_token:
            try:
                existing_click = AffiliateClick.objects.filter(
                    attribution_token=existing_token,
                    affiliate=account,
                    created_at__gte=timezone.now() - timedelta(
                        days=int(getattr(settings, "AFFILIATE_ATTRIBUTION_WINDOW_DAYS", 30))
                    ),
                ).first()
            except (TypeError, ValueError):
                existing_click = None
            if existing_click:
                return Response({
                    "referral_code": account.referral_code,
                    "attribution_token": str(existing_click.attribution_token),
                    "recorded": False,
                })

        ip = _client_ip(request)
        ip_hash = hashlib.sha256(f"{settings.SECRET_KEY}:{ip}".encode()).hexdigest() if ip else ""
        user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]
        dedup_minutes = int(getattr(settings, "AFFILIATE_CLICK_DEDUP_MINUTES", 30))
        if ip_hash:
            recent_click = AffiliateClick.objects.filter(
                affiliate=account,
                ip_hash=ip_hash,
                user_agent=user_agent,
                created_at__gte=timezone.now() - timedelta(minutes=dedup_minutes),
            ).first()
            if recent_click:
                return Response({
                    "referral_code": account.referral_code,
                    "attribution_token": str(recent_click.attribution_token),
                    "recorded": False,
                })

        token = uuid.uuid4()
        AffiliateClick.objects.create(
            affiliate=account,
            attribution_token=token,
            source=(request.data.get("source") or "Direct")[:100],
            landing_url=(request.data.get("landing_url") or "")[:1000],
            referrer=(request.data.get("referrer") or "")[:1000],
            ip_hash=ip_hash,
            user_agent=user_agent,
        )
        return Response({
            "referral_code": account.referral_code,
            "attribution_token": str(token),
            "recorded": True,
        })


class AffiliateDashboardView(APIView):
    permission_classes = AFFILIATE_PERMS

    def get(self, request):
        account = request.user.affiliate_account
        clicks = account.clicks.count()
        referrals = account.referrals.all()
        signups = referrals.count()
        paid = referrals.filter(status="paid").count()
        earnings = referrals.filter(status__in=("approved", "paid")).aggregate(total=Sum("reward_amount"))["total"] or Decimal("0")
        recent = [
            {"date": item.created_at.date().isoformat(), "event": f"{item.company_name or item.signup_email} signed up"}
            for item in referrals[:8]
        ]
        return Response({
            "greeting_name": request.user.first_name or request.user.email,
            "referral_code": account.referral_code,
            "referral_link": f"{getattr(settings, 'FRONTEND_URL', 'https://netily.co.ke').rstrip('/')}/affiliate/{account.referral_code}",
            "stats": {
                "link_views": clicks,
                "signed_up": signups,
                "paid": paid,
                "conversion_rate": round((signups / clicks * 100), 1) if clicks else 0,
                "total_earnings": float(earnings),
                "currency": account.currency,
            },
            "funnel": {"link_clicks": clicks, "page_views": clicks, "signups": signups, "paid": paid},
            "recent_activity": recent,
        })


class AffiliateReferralListView(APIView):
    permission_classes = AFFILIATE_PERMS

    def get(self, request):
        referrals = request.user.affiliate_account.referrals.select_related("click", "lead")
        return Response([referral_data(item) for item in referrals])


class AffiliateAnalyticsView(APIView):
    permission_classes = AFFILIATE_PERMS

    def get(self, request):
        account = request.user.affiliate_account
        period = request.query_params.get("period", "30d")
        start = _period_start(period)
        click_daily = {
            str(row["day"]): row["count"]
            for row in account.clicks.filter(created_at__gte=start).annotate(day=TruncDate("created_at")).values("day").annotate(count=Count("id"))
        }
        signup_daily = {
            str(row["day"]): row["count"]
            for row in account.referrals.filter(created_at__gte=start).annotate(day=TruncDate("created_at")).values("day").annotate(count=Count("id"))
        }
        paid_daily = {
            str(row["day"]): row["count"]
            for row in account.referrals.filter(created_at__gte=start, status="paid").annotate(day=TruncDate("created_at")).values("day").annotate(count=Count("id"))
        }
        days = []
        cursor = start.date()
        while cursor <= timezone.localdate():
            key = str(cursor)
            days.append({"date": key, "views": click_daily.get(key, 0), "signups": signup_daily.get(key, 0), "paid": paid_daily.get(key, 0)})
            cursor += timedelta(days=1)
        clicks = sum(click_daily.values())
        signups = sum(signup_daily.values())
        paid = sum(paid_daily.values())
        earnings = account.referrals.filter(
            created_at__gte=start,
            status__in=("approved", "paid"),
        ).aggregate(total=Sum("reward_amount"))["total"] or 0
        return Response({
            "period": period,
            "link_views": clicks,
            "signups": signups,
            "paid": paid,
            "conversion_rate": round(signups / clicks * 100, 1) if clicks else 0,
            "epc": round(float(earnings) / clicks, 2) if clicks else 0,
            "daily": days,
        })


class AffiliateTrafficView(APIView):
    permission_classes = AFFILIATE_PERMS

    def get(self, request):
        start = _period_start(request.query_params.get("period", "30d"))
        rows = request.user.affiliate_account.clicks.filter(created_at__gte=start).values("source").annotate(
            clicks=Count("id"),
            signups=Count("signups", distinct=True),
        ).order_by("-clicks")
        return Response([
            {
                "source": row["source"],
                "clicks": row["clicks"],
                "signups": row["signups"],
                "conversion_rate": round(row["signups"] / row["clicks"] * 100, 1) if row["clicks"] else 0,
            }
            for row in rows
        ])


class AffiliatePayoutListView(APIView):
    permission_classes = AFFILIATE_PERMS

    def get(self, request):
        account = request.user.affiliate_account
        earned = account.referrals.filter(status__in=("approved", "paid")).aggregate(total=Sum("reward_amount"))["total"] or Decimal("0")
        paid = account.payouts.filter(status="completed").aggregate(total=Sum("amount"))["total"] or Decimal("0")
        return Response({
            "total_earned": float(earned),
            "pending": float(max(earned - paid, Decimal("0"))),
            "paid_out": float(paid),
            "currency": account.currency,
            "history": [payout_data(item) for item in account.payouts.all()],
        })


class AffiliatePaymentMethodView(APIView):
    permission_classes = AFFILIATE_PERMS

    def get(self, request):
        return Response(self._data(request.user.affiliate_account))

    def patch(self, request):
        account = request.user.affiliate_account
        allowed = {"payment_method", "mpesa_phone", "mpesa_name", "bank_name", "bank_account", "bank_branch"}
        for key, value in request.data.items():
            if key in allowed:
                setattr(account, key, str(value).strip())
        account.payment_verified = False
        account.save()
        return Response(self._data(account))

    def _data(self, account):
        return {
            "type": account.payment_method,
            "mpesa_phone": account.mpesa_phone,
            "mpesa_name": account.mpesa_name,
            "bank_name": account.bank_name,
            "bank_account": account.bank_account,
            "bank_branch": account.bank_branch,
            "is_verified": account.payment_verified,
        }


class AffiliateTierView(APIView):
    permission_classes = AFFILIATE_PERMS

    def get(self, request):
        account = request.user.affiliate_account
        count = account.referrals.count()
        return Response({
            "current_tier": account.tier,
            "referrals_count": count,
            "tiers": [
                {"name": "Bronze", "key": "bronze", "min_referrals": 0, "max_referrals": None, "reward_per_referral": 0, "currency": account.currency, "unlocked": account.tier == "bronze"},
                {"name": "Silver", "key": "silver", "min_referrals": 0, "max_referrals": None, "reward_per_referral": 0, "currency": account.currency, "unlocked": account.tier == "silver"},
                {"name": "Gold", "key": "gold", "min_referrals": 0, "max_referrals": None, "reward_per_referral": 0, "currency": account.currency, "unlocked": account.tier == "gold"},
            ],
            "next_tier_remaining": None,
        })


class AffiliateMarketingView(APIView):
    permission_classes = AFFILIATE_PERMS

    def get(self, request):
        link = f"{getattr(settings, 'FRONTEND_URL', 'https://netily.co.ke').rstrip('/')}/affiliate/{request.user.affiliate_account.referral_code}"
        return Response([
            {"id": 1, "category": "whatsapp", "title": "Affiliate introduction", "content": f"Manage your ISP with Netily: {link}"},
            {"id": 2, "category": "social", "title": "Social post", "content": f"Modern billing, RADIUS and network operations for ISPs: {link}"},
        ])


def _admin_affiliate_data(account):
    earned = account.referrals.filter(status__in=("approved", "paid")).aggregate(total=Sum("reward_amount"))["total"] or 0
    return {
        **affiliate_user_data(account),
        "referrals_count": account.referrals.count(),
        "total_earned": float(earned),
        "payment_method": account.get_payment_method_display(),
        "referrals": [referral_data(item) for item in account.referrals.all()],
        "payouts": [payout_data(item) for item in account.payouts.all()],
    }


class AdminAffiliateListView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        qs = AffiliateAccount.objects.select_related("user").prefetch_related("referrals__click", "referrals__lead", "payouts")
        search = (request.query_params.get("search") or "").strip()
        state = (request.query_params.get("status") or "").strip()
        if search:
            qs = qs.filter(Q(user__email__icontains=search) | Q(user__first_name__icontains=search) | Q(user__last_name__icontains=search) | Q(referral_code__icontains=search))
        if state and state != "all":
            qs = qs.filter(status=state)
        return Response([_admin_affiliate_data(item) for item in qs])

    def post(self, request):
        _ensure_public()
        serializer = AffiliateAccountAdminCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = serializer.save()
        if not account.is_verified:
            _send_verification(account)
        _audit_admin(
            request,
            "affiliate_account_created",
            f"Created affiliate account {account.referral_code}",
            account,
            {"email": account.user.email, "status": account.status},
        )
        return Response(_admin_affiliate_data(account), status=status.HTTP_201_CREATED)


class AdminAffiliateDetailView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request, pk):
        _ensure_public()
        account = _affiliate_or_404(pk)
        if not account:
            return Response({"detail": "Affiliate not found."}, status=404)
        return Response(_admin_affiliate_data(account))

    def patch(self, request, pk):
        _ensure_public()
        account = _affiliate_or_404(pk)
        if not account:
            return Response({"detail": "Affiliate not found."}, status=404)
        serializer = AffiliateAccountAdminSerializer(account, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        _audit_admin(
            request,
            "affiliate_account_updated",
            f"Updated affiliate account {account.referral_code}",
            account,
            {"fields": sorted(serializer.validated_data.keys())},
        )
        return Response(_admin_affiliate_data(account))

    def delete(self, request, pk):
        _ensure_public()
        account = _affiliate_or_404(pk)
        if not account:
            return Response({"detail": "Affiliate not found."}, status=404)
        account.status = "inactive"
        account.save(update_fields=["status", "updated_at"])
        account.user.is_active = False
        account.user.save(update_fields=["is_active"])
        _audit_admin(
            request,
            "affiliate_account_deactivated",
            f"Deactivated affiliate account {account.referral_code}",
            account,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminAffiliateReferralListView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, affiliate_id):
        _ensure_public()
        account = _affiliate_or_404(affiliate_id)
        if not account:
            return Response({"detail": "Affiliate not found."}, status=404)
        serializer = AffiliateReferralAdminCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        referral = serializer.save(affiliate=account, currency=account.currency)
        _audit_admin(
            request,
            "affiliate_referral_created_manually",
            f"Created manual referral {referral.id} for {account.referral_code}",
            account,
            {"referral_id": referral.id, "signup_email": referral.signup_email},
        )
        return Response(referral_data(referral), status=status.HTTP_201_CREATED)


class AdminReferralDetailView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def patch(self, request, pk):
        _ensure_public()
        referral = AffiliateReferral.objects.select_related("affiliate__user").filter(pk=pk).first()
        if not referral:
            return Response({"detail": "Referral not found."}, status=404)
        serializer = AffiliateReferralAdminSerializer(referral, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(currency=referral.affiliate.currency)
        _audit_admin(
            request,
            "affiliate_commission_updated",
            f"Updated referral {referral.id} for {referral.affiliate.referral_code}",
            referral.affiliate,
            {
                "referral_id": referral.id,
                "status": referral.status,
                "reward_amount": str(referral.reward_amount),
                "currency": referral.currency,
            },
        )
        return Response(referral_data(referral))


class AdminPayoutListView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, affiliate_id):
        _ensure_public()
        account = _affiliate_or_404(affiliate_id)
        if not account:
            return Response({"detail": "Affiliate not found."}, status=404)
        serializer = AffiliatePayoutAdminSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        requested_amount = serializer.validated_data["amount"]
        requested_status = serializer.validated_data.get("status", "pending")
        reference = serializer.validated_data.get("reference", "").strip()
        eligible = (
            account.referrals.filter(status__in=("approved", "paid"))
            .aggregate(total=Sum("reward_amount"))["total"]
            or Decimal("0")
        )
        completed = (
            account.payouts.filter(status="completed").aggregate(total=Sum("amount"))["total"]
            or Decimal("0")
        )
        if requested_status == "completed" and not reference:
            return Response({"detail": "A transaction reference is required for a completed payout."}, status=400)
        if requested_status == "completed" and completed + requested_amount > eligible:
            return Response(
                {"detail": f"Completed payouts cannot exceed approved commission balance ({account.currency} {eligible - completed})."},
                status=400,
            )
        payout = serializer.save(affiliate=account, created_by=request.user, currency=account.currency)
        if payout.status == "completed":
            payout.processed_at = timezone.now()
            payout.save(update_fields=["processed_at", "updated_at"])
        _audit_admin(
            request,
            "affiliate_payout_recorded",
            f"Recorded affiliate payout {payout.id} for {account.referral_code}",
            account,
            {"payout_id": payout.id, "amount": str(payout.amount), "currency": payout.currency, "status": payout.status},
        )
        return Response(payout_data(payout), status=201)


class AdminPayoutDetailView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def patch(self, request, pk):
        _ensure_public()
        payout = AffiliatePayout.objects.select_related("affiliate__user").filter(pk=pk).first()
        if not payout:
            return Response({"detail": "Payout not found."}, status=404)
        serializer = AffiliatePayoutAdminSerializer(payout, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        next_status = serializer.validated_data.get("status", payout.status)
        next_amount = serializer.validated_data.get("amount", payout.amount)
        next_reference = serializer.validated_data.get("reference", payout.reference).strip()
        if next_status == "completed" and not next_reference:
            return Response({"detail": "A transaction reference is required for a completed payout."}, status=400)
        if next_status == "completed":
            eligible = (
                payout.affiliate.referrals.filter(status__in=("approved", "paid"))
                .aggregate(total=Sum("reward_amount"))["total"]
                or Decimal("0")
            )
            other_completed = (
                payout.affiliate.payouts.filter(status="completed")
                .exclude(pk=payout.pk)
                .aggregate(total=Sum("amount"))["total"]
                or Decimal("0")
            )
            if other_completed + next_amount > eligible:
                return Response({"detail": "Completed payouts cannot exceed approved commission balance."}, status=400)
        payout = serializer.save(currency=payout.affiliate.currency)
        payout.processed_at = timezone.now() if payout.status == "completed" else None
        payout.save(update_fields=["processed_at", "updated_at"])
        _audit_admin(
            request,
            "affiliate_payout_updated",
            f"Updated affiliate payout {payout.id}",
            payout.affiliate,
            {"payout_id": payout.id, "status": payout.status, "reference": payout.reference},
        )
        return Response(payout_data(payout))


class AdminAffiliateSettingsView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        settings_obj = GlobalSystemSettings.get_solo()
        return Response({"affiliate_email_otp_enabled": settings_obj.affiliate_email_otp_enabled})

    def patch(self, request):
        _ensure_public()
        settings_obj = GlobalSystemSettings.get_solo()
        enabled = request.data.get("affiliate_email_otp_enabled")
        if not isinstance(enabled, bool):
            return Response({"detail": "affiliate_email_otp_enabled must be true or false."}, status=400)
        previous = settings_obj.affiliate_email_otp_enabled
        settings_obj.affiliate_email_otp_enabled = enabled
        settings_obj.save(update_fields=["affiliate_email_otp_enabled"])
        SuperAdminActivityLog.objects.create(
            actor=request.user,
            action="affiliate_otp_setting_updated",
            summary=f"Affiliate login OTP {'enabled' if enabled else 'disabled'}",
            metadata={"old": previous, "new": enabled},
            ip_address=_client_ip(request) or None,
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
        )
        return Response({"affiliate_email_otp_enabled": enabled})


class AdminAffiliateAccessView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, pk):
        _ensure_public()
        account = _affiliate_or_404(pk)
        if not account:
            return Response({"detail": "Affiliate not found."}, status=404)
        if account.status != "active" or not account.user.is_active:
            return Response({"detail": "Activate this affiliate before opening its account."}, status=409)
        nonce = uuid.uuid4().hex
        cache.set(
            f"affiliate_admin_access:{nonce}",
            {"affiliate_id": account.id, "actor_id": request.user.id},
            timeout=120,
        )
        token = signing.dumps({"nonce": nonce}, salt=ADMIN_ACCESS_SALT)
        frontend = getattr(settings, "FRONTEND_URL", "https://netily.co.ke").rstrip("/")
        _audit_admin(
            request,
            "affiliate_account_access_requested",
            f"Requested temporary access to affiliate {account.referral_code}",
            account,
        )
        return Response({"access_url": f"{frontend}/affiliate/admin-access?token={token}", "expires_in": 120})


class AdminAffiliatePasswordView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, pk):
        _ensure_public()
        account = _affiliate_or_404(pk)
        if not account:
            return Response({"detail": "Affiliate not found."}, status=404)
        mode = (request.data.get("mode") or "manual").strip().lower()
        send_email = bool(request.data.get("send_email", False))
        if mode == "temporary":
            temporary_password = _generate_temporary_password()
            try:
                validate_password(temporary_password, user=account.user)
            except ValidationError:
                temporary_password = _generate_temporary_password(18)
            frontend = getattr(settings, "FRONTEND_URL", "https://netily.co.ke").rstrip("/")
            plain_message = (
                "Your Netily affiliate temporary password has been issued.\n\n"
                f"Email: {account.user.email}\n"
                f"Temporary password: {temporary_password}\n\n"
                f"Open {frontend}/affiliate/login, choose temporary password reset, and set a new password. "
                "This temporary password is valid for 24 hours. If you did not request this, contact support immediately."
            )
            html_message = (
                "<h2>Your Netily affiliate temporary password</h2>"
                "<p>A temporary password has been issued for your affiliate account.</p>"
                f"<p><strong>Email:</strong> {account.user.email}</p>"
                f"<p><strong>Temporary password:</strong> <code>{temporary_password}</code></p>"
                f'<p><a href="{frontend}/affiliate/login">Open affiliate login</a>, choose temporary password reset, and set a new password.</p>'
                "<p>This temporary password is valid for 24 hours.</p>"
            )
            delivery = _send_affiliate_password_email(
                account=account,
                subject="Your Netily affiliate temporary password",
                plain_message=plain_message,
                html_message=html_message,
            )
            if not delivery.get("sent"):
                return Response({"detail": "Temporary password email could not be delivered. No password was changed."}, status=503)
            account.user.set_password(temporary_password)
            account.user.is_active = True
            account.user.save(update_fields=["password", "is_active"])
            cache.set(f"{AFFILIATE_TEMP_PASSWORD_CACHE_PREFIX}:{account.user_id}", True, timeout=86400)
            _audit_admin(
                request,
                "affiliate_temporary_password_sent",
                f"Sent temporary password to affiliate {account.referral_code}",
                account,
                {"email": account.user.email},
            )
            return Response({"detail": "Temporary password sent to the affiliate email."})

        new_password = request.data.get("new_password") or ""
        confirm_password = request.data.get("confirm_password") or ""
        if new_password != confirm_password:
            return Response({"detail": "Passwords do not match."}, status=400)
        try:
            validate_password(new_password, user=account.user)
        except ValidationError as exc:
            return Response({"detail": " ".join(exc.messages)}, status=400)
        account.user.set_password(new_password)
        account.user.is_active = True
        account.user.save(update_fields=["password", "is_active"])
        cache.delete(f"{AFFILIATE_TEMP_PASSWORD_CACHE_PREFIX}:{account.user_id}")
        if send_email:
            frontend = getattr(settings, "FRONTEND_URL", "https://netily.co.ke").rstrip("/")
            _send_affiliate_password_email(
                account=account,
                subject="Your Netily affiliate password was changed",
                plain_message=(
                    "Your Netily affiliate password was changed by the platform admin.\n\n"
                    f"You can sign in here: {frontend}/affiliate/login\n\n"
                    "If this was unexpected, contact support immediately."
                ),
                html_message=(
                    "<h2>Your Netily affiliate password was changed</h2>"
                    "<p>Your password was changed by the platform admin.</p>"
                    f'<p><a href="{frontend}/affiliate/login">Sign in to your affiliate account</a></p>'
                    "<p>If this was unexpected, contact support immediately.</p>"
                ),
            )
        _audit_admin(
            request,
            "affiliate_password_changed",
            f"Changed password for affiliate {account.referral_code}",
            account,
            {"email": account.user.email, "email_notified": send_email},
        )
        return Response({"detail": "Affiliate password changed."})


class AffiliateAdminAccessExchangeView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "affiliate_login"

    def post(self, request):
        _ensure_public()
        try:
            payload = signing.loads(request.data.get("token", ""), salt=ADMIN_ACCESS_SALT, max_age=120)
            nonce = payload["nonce"]
            grant = cache.get(f"affiliate_admin_access:{nonce}")
            if not grant:
                raise signing.BadSignature("Grant is missing or already used.")
            if not cache.add(f"affiliate_admin_access_used:{nonce}", True, timeout=120):
                raise signing.BadSignature("Grant was already used.")
            cache.delete(f"affiliate_admin_access:{nonce}")
            actor = User.objects.get(pk=grant["actor_id"], is_superuser=True, is_active=True)
            account = AffiliateAccount.objects.select_related("user").get(
                pk=grant["affiliate_id"],
                status="active",
                user__is_active=True,
            )
        except (signing.BadSignature, signing.SignatureExpired, KeyError, User.DoesNotExist, AffiliateAccount.DoesNotExist):
            return Response({"detail": "This temporary access link is invalid or expired."}, status=400)
        access = AccessToken.for_user(account.user)
        access["impersonated_by"] = str(actor.id)
        access.set_exp(lifetime=timedelta(minutes=15))
        SuperAdminActivityLog.objects.create(
            actor=actor,
            target_user=account.user,
            action="affiliate_account_access_exchanged",
            summary=f"Opened temporary access to affiliate {account.referral_code}",
            metadata={"affiliate_id": account.id, "expires_in": 900},
            ip_address=_client_ip(request) or None,
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
        )
        return Response({"access": str(access), "user": affiliate_user_data(account), "expires_in": 900})


class AdminAffiliateExportView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Name", "Email", "Code", "Referrals", "Total Earned", "Currency", "Status", "Tier"])
        for account in AffiliateAccount.objects.select_related("user"):
            writer.writerow([
                account.user.get_full_name(),
                account.user.email,
                account.referral_code,
                account.referrals.count(),
                account.referrals.filter(status__in=("approved", "paid")).aggregate(total=Sum("reward_amount"))["total"] or 0,
                account.currency,
                account.status,
                account.tier,
            ])
        response = HttpResponse(output.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="netily-affiliates.csv"'
        return response
