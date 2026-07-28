import csv
import hashlib
import ipaddress
import io
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import authenticate
from django.core import signing
from django.core.mail import send_mail
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

from .models import AffiliateAccount, AffiliateClick, AffiliatePayout, AffiliateReferral
from .permissions import IsActiveAffiliate
from .serializers import (
    AffiliateAccountAdminSerializer,
    AffiliatePayoutAdminSerializer,
    AffiliateReferralAdminSerializer,
    AffiliateRegisterSerializer,
    affiliate_user_data,
    payout_data,
    referral_data,
)

AFFILIATE_PERMS = [IsAuthenticated, IsActiveAffiliate]
SUPERADMIN_PERMS = [IsAuthenticated, IsSuperAdmin]
VERIFICATION_SALT = "affiliate-email-verification"


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
    send_mail(
        "Verify your Netily affiliate account",
        f"Verify your affiliate email by opening this link:\n\n{url}\n\nThis link expires in 24 hours.",
        settings.DEFAULT_FROM_EMAIL,
        [account.user.email],
        fail_silently=True,
    )


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
        _send_verification(account)
        return Response({"user": affiliate_user_data(account)}, status=status.HTTP_201_CREATED)


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

        requires_otp = not OTPService.is_otp_exempt_user(user)
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
            _send_verification(account)
        return Response({"detail": "If the account exists, a verification email has been sent."})


class AffiliateClickView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "affiliate_click"

    def post(self, request, code):
        _ensure_public()
        account = AffiliateAccount.objects.filter(referral_code=code.upper(), status="active").first()
        if not account:
            return Response({"detail": "Referral link not found."}, status=404)
        token = uuid.uuid4()
        ip = _client_ip(request)
        AffiliateClick.objects.create(
            affiliate=account,
            attribution_token=token,
            source=(request.data.get("source") or "Direct")[:100],
            landing_url=(request.data.get("landing_url") or "")[:1000],
            referrer=(request.data.get("referrer") or "")[:1000],
            ip_hash=hashlib.sha256(f"{settings.SECRET_KEY}:{ip}".encode()).hexdigest() if ip else "",
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
        )
        return Response({"referral_code": account.referral_code, "attribution_token": str(token)})


class AffiliateDashboardView(APIView):
    permission_classes = AFFILIATE_PERMS

    def get(self, request):
        account = request.user.affiliate_account
        clicks = account.clicks.count()
        referrals = account.referrals.all()
        signups = referrals.count()
        paid = referrals.filter(status="paid").count()
        earnings = referrals.aggregate(total=Sum("reward_amount"))["total"] or Decimal("0")
        recent = [
            {"date": item.created_at.date().isoformat(), "event": f"{item.company_name or item.signup_email} signed up"}
            for item in referrals[:8]
        ]
        return Response({
            "greeting_name": request.user.first_name or request.user.email,
            "referral_code": account.referral_code,
            "referral_link": f"{getattr(settings, 'FRONTEND_URL', 'https://netily.co.ke').rstrip('/')}/r/{account.referral_code}",
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
        return Response([referral_data(item) for item in request.user.affiliate_account.referrals.all()])


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
        earnings = account.referrals.filter(created_at__gte=start).aggregate(total=Sum("reward_amount"))["total"] or 0
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
        earned = account.referrals.aggregate(total=Sum("reward_amount"))["total"] or Decimal("0")
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
        link = f"{getattr(settings, 'FRONTEND_URL', 'https://netily.co.ke').rstrip('/')}/r/{request.user.affiliate_account.referral_code}"
        return Response([
            {"id": 1, "category": "whatsapp", "title": "Affiliate introduction", "content": f"Manage your ISP with Netily: {link}"},
            {"id": 2, "category": "social", "title": "Social post", "content": f"Modern billing, RADIUS and network operations for ISPs: {link}"},
        ])


def _admin_affiliate_data(account):
    earned = account.referrals.aggregate(total=Sum("reward_amount"))["total"] or 0
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
        qs = AffiliateAccount.objects.select_related("user").prefetch_related("referrals", "payouts")
        search = (request.query_params.get("search") or "").strip()
        state = (request.query_params.get("status") or "").strip()
        if search:
            qs = qs.filter(Q(user__email__icontains=search) | Q(user__first_name__icontains=search) | Q(user__last_name__icontains=search) | Q(referral_code__icontains=search))
        if state and state != "all":
            qs = qs.filter(status=state)
        return Response([_admin_affiliate_data(item) for item in qs])


class AdminAffiliateDetailView(APIView):
    permission_classes = SUPERADMIN_PERMS

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


class AdminReferralDetailView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def patch(self, request, pk):
        _ensure_public()
        referral = AffiliateReferral.objects.select_related("affiliate__user").filter(pk=pk).first()
        if not referral:
            return Response({"detail": "Referral not found."}, status=404)
        serializer = AffiliateReferralAdminSerializer(referral, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
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
        payout = serializer.save(affiliate=account, created_by=request.user)
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
        payout = serializer.save()
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
                account.referrals.aggregate(total=Sum("reward_amount"))["total"] or 0,
                account.currency,
                account.status,
                account.tier,
            ])
        response = HttpResponse(output.getvalue(), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="netily-affiliates.csv"'
        return response
