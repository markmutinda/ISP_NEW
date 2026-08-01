from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import authenticate
from django.db import IntegrityError, transaction, connection
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from apps.core.session_tokens import issue_refresh_token

from apps.core.models import Lead, Tenant, User
from apps.superadmin.models import SuperAdminActivityLog, SupportActivityLog, SupportExecutiveProfile
from apps.superadmin.permissions import IsPlatformSupport, IsSuperAdmin
from apps.superadmin.serializers import (
    SuperAdminAccountSerializer,
    SuperAdminActivityLogSerializer,
    SupportActivityLogSerializer,
    SupportExecutiveSerializer,
)
from apps.superadmin.tasks import revoke_superadmin_tenant_mirrors

try:
    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
except ImportError:
    OutstandingToken = None
    BlacklistedToken = None


SUPPORT_PERMS = [IsAuthenticated, IsPlatformSupport]
SUPERADMIN_PERMS = [IsAuthenticated, IsSuperAdmin]


def _ensure_public():
    if hasattr(connection, "set_schema_to_public"):
        connection.set_schema_to_public()


def _client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def record_support_activity(request, action, summary, area="", metadata=None):
    SupportActivityLog.objects.create(
        support_user=request.user if request.user.is_authenticated else None,
        action=action,
        area=area,
        summary=summary[:255],
        metadata=metadata or {},
        ip_address=_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
    )


def record_superadmin_activity(request, action, summary, target_user=None, metadata=None):
    SuperAdminActivityLog.objects.create(
        actor=request.user if request.user.is_authenticated else None,
        target_user=target_user,
        action=action,
        summary=summary[:255],
        metadata=metadata or {},
        ip_address=_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
    )


def _active_superadmin_count(exclude_user_id=None):
    qs = User.objects.filter(is_superuser=True, is_active=True)
    if exclude_user_id:
        qs = qs.exclude(pk=exclude_user_id)
    return qs.count()


def _support_user_payload(user):
    profile = getattr(user, "platform_support_profile", None)
    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "is_superuser": user.is_superuser,
        "role": user.role,
        "support_profile": SupportExecutiveSerializer(profile).data if profile else None,
    }


def _serialize_lead(lead):
    from apps.affiliate.services import affiliate_lead_data

    return {
        "id": lead.id,
        "name": lead.name,
        "email": lead.email,
        "phone": lead.phone,
        "company_name": lead.company_name,
        "lead_source": lead.lead_source,
        "referral_name": lead.referral_name,
        "affiliate_referral": affiliate_lead_data(lead),
        "message": lead.message,
        "is_contacted": lead.is_contacted,
        "contacted_at": lead.contacted_at.isoformat() if lead.contacted_at else None,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
    }


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class SupportConsoleLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        _ensure_public()
        email = (request.data.get("email") or "").strip().lower()
        password = request.data.get("password") or ""

        user = authenticate(email=email, password=password)
        if not user:
            user = authenticate(request=request, username=email, password=password)

        if not user or not user.is_active:
            return Response({"detail": "Invalid support credentials."}, status=status.HTTP_400_BAD_REQUEST)

        profile = getattr(user, "platform_support_profile", None)
        if not user.is_superuser and not (profile and profile.is_active):
            return Response(
                {"detail": "This account has not been enabled for Netily Support."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if profile:
            profile.last_seen_at = timezone.now()
            profile.save(update_fields=["last_seen_at", "updated_at"])

        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])

        refresh = issue_refresh_token(user)
        SupportActivityLog.objects.create(
            support_user=user,
            action="login",
            area="auth",
            summary="Signed in to support console",
            ip_address=_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
        )
        return Response({
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": _support_user_payload(user),
        })


class SupportConsoleMeView(APIView):
    permission_classes = SUPPORT_PERMS

    def get(self, request):
        _ensure_public()
        profile = getattr(request.user, "platform_support_profile", None)
        if profile:
            profile.last_seen_at = timezone.now()
            profile.save(update_fields=["last_seen_at", "updated_at"])
        return Response(_support_user_payload(request.user))


class SupportConsoleDashboardView(APIView):
    permission_classes = SUPPORT_PERMS

    def get(self, request):
        _ensure_public()
        now = timezone.now()
        lead_qs = Lead.objects.all()
        my_logs = SupportActivityLog.objects.filter(support_user=request.user)

        return Response({
            "stats": {
                "total_leads": lead_qs.count(),
                "open_leads": lead_qs.filter(is_contacted=False).count(),
                "contacted_leads": lead_qs.filter(is_contacted=True).count(),
                "new_leads_7_days": lead_qs.filter(created_at__gte=now - timedelta(days=7)).count(),
                "active_tenants": Tenant.objects.exclude(schema_name="public").filter(status__in=["trial", "active"]).count(),
                "my_actions_today": my_logs.filter(created_at__date=now.date()).count(),
            },
            "recent_leads": [_serialize_lead(lead) for lead in lead_qs.order_by("-created_at")[:6]],
            "recent_activity": SupportActivityLogSerializer(my_logs[:8], many=True).data,
        })


class SupportConsoleActivityView(APIView):
    permission_classes = SUPPORT_PERMS

    def get(self, request):
        _ensure_public()
        logs = SupportActivityLog.objects.filter(support_user=request.user)[:50]
        return Response(SupportActivityLogSerializer(logs, many=True).data)

    def post(self, request):
        _ensure_public()
        action = (request.data.get("action") or "note").strip()[:80]
        area = (request.data.get("area") or "support").strip()[:80]
        summary = (request.data.get("summary") or "Support action recorded").strip()
        metadata = request.data.get("metadata") if isinstance(request.data.get("metadata"), dict) else {}
        record_support_activity(request, action, summary, area, metadata)
        return Response({"detail": "Activity recorded."}, status=status.HTTP_201_CREATED)


class SupportConsoleLeadListView(APIView):
    permission_classes = SUPPORT_PERMS

    def get(self, request):
        _ensure_public()
        search = (request.query_params.get("search") or "").strip()
        contacted = request.query_params.get("contacted")
        page = max(int(request.query_params.get("page", 1)), 1)
        page_size = min(max(int(request.query_params.get("page_size", 20)), 1), 100)

        qs = Lead.objects.prefetch_related(
            "affiliatereferral_set__affiliate__user"
        ).order_by("-created_at")
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(email__icontains=search)
                | Q(phone__icontains=search)
                | Q(company_name__icontains=search)
                | Q(referral_name__icontains=search)
                | Q(affiliatereferral__affiliate__referral_code__icontains=search)
                | Q(affiliatereferral__affiliate__user__email__icontains=search)
            ).distinct()
        if contacted == "true":
            qs = qs.filter(is_contacted=True)
        elif contacted == "false":
            qs = qs.filter(is_contacted=False)

        total = qs.count()
        start = (page - 1) * page_size
        results = [_serialize_lead(lead) for lead in qs[start:start + page_size]]
        return Response({
            "count": total,
            "next": None if start + page_size >= total else page + 1,
            "previous": None if page <= 1 else page - 1,
            "results": results,
        })

    def post(self, request):
        _ensure_public()
        name = (request.data.get("name") or "").strip()
        email = (request.data.get("email") or "").strip()
        if not name or not email:
            return Response({"detail": "Lead name and email are required."}, status=status.HTTP_400_BAD_REQUEST)

        lead = Lead.objects.create(
            name=name,
            email=email,
            phone=(request.data.get("phone") or "").strip(),
            company_name=(request.data.get("company_name") or "").strip(),
            lead_source=(request.data.get("lead_source") or "support_console").strip(),
            referral_name=(request.data.get("referral_name") or "").strip(),
            message=(request.data.get("message") or "").strip(),
        )
        record_support_activity(
            request,
            "lead_created",
            f"Created lead for {lead.company_name or lead.name}",
            "leads",
            {"lead_id": lead.id},
        )
        return Response(_serialize_lead(lead), status=status.HTTP_201_CREATED)


class SupportConsoleLeadDetailView(APIView):
    permission_classes = SUPPORT_PERMS

    def patch(self, request, pk):
        _ensure_public()
        try:
            lead = Lead.objects.get(pk=pk)
        except Lead.DoesNotExist:
            return Response({"detail": "Lead not found."}, status=status.HTTP_404_NOT_FOUND)

        updated_fields = []
        allowed_fields = ["name", "email", "phone", "company_name", "lead_source", "referral_name", "message"]
        for field in allowed_fields:
            if field in request.data:
                setattr(lead, field, (request.data.get(field) or "").strip())
                updated_fields.append(field)

        if "is_contacted" in request.data:
            lead.is_contacted = _as_bool(request.data.get("is_contacted"))
            lead.contacted_at = timezone.now() if lead.is_contacted else None
            updated_fields.extend(["is_contacted", "contacted_at"])

        if updated_fields:
            lead.save(update_fields=updated_fields)
            record_support_activity(
                request,
                "lead_updated",
                f"Updated lead {lead.company_name or lead.name}",
                "leads",
                {"lead_id": lead.id, "fields": updated_fields},
            )
        return Response(_serialize_lead(lead))


class SuperadminSupportExecutiveListCreateView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        profiles = SupportExecutiveProfile.objects.select_related("user", "created_by")
        return Response(SupportExecutiveSerializer(profiles, many=True).data)

    def post(self, request):
        _ensure_public()
        email = (request.data.get("email") or "").strip().lower()
        first_name = (request.data.get("first_name") or "").strip()
        last_name = (request.data.get("last_name") or "").strip()
        phone_number = (request.data.get("phone_number") or "").strip()
        password = request.data.get("password") or ""

        if not email or not password or not phone_number:
            return Response(
                {"detail": "Email, phone number, and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if User.objects.filter(email__iexact=email).exists():
            return Response({"detail": "A user with this email already exists."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    email=email,
                    password=password,
                    first_name=first_name,
                    last_name=last_name,
                    phone_number=phone_number,
                    role="support",
                    is_staff=True,
                    is_active=_as_bool(request.data.get("is_active"), True),
                )
                profile = SupportExecutiveProfile.objects.create(
                    user=user,
                    title=(request.data.get("title") or "Customer Support Executive").strip(),
                    phone_number=phone_number,
                    can_register_tenants=_as_bool(request.data.get("can_register_tenants"), True),
                    can_manage_leads=_as_bool(request.data.get("can_manage_leads"), True),
                    can_view_tenants=_as_bool(request.data.get("can_view_tenants"), True),
                    is_active=_as_bool(request.data.get("is_active"), True),
                    created_by=request.user,
                )
        except IntegrityError:
            return Response(
                {"detail": "Could not create support account. Check that the email and phone number are unique."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        record_support_activity(
            request,
            "support_account_created",
            f"Created support account for {email}",
            "support_accounts",
            {"support_user_id": user.id},
        )
        return Response(SupportExecutiveSerializer(profile).data, status=status.HTTP_201_CREATED)


class SuperadminSupportExecutiveDetailView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def patch(self, request, user_id):
        _ensure_public()
        try:
            profile = SupportExecutiveProfile.objects.select_related("user").get(user_id=user_id)
        except SupportExecutiveProfile.DoesNotExist:
            return Response({"detail": "Support executive not found."}, status=status.HTTP_404_NOT_FOUND)

        user = profile.user
        for field in ["first_name", "last_name"]:
            if field in request.data:
                setattr(user, field, (request.data.get(field) or "").strip())
        if "email" in request.data:
            email = (request.data.get("email") or "").strip().lower()
            if email and User.objects.exclude(pk=user.pk).filter(email__iexact=email).exists():
                return Response({"detail": "A user with this email already exists."}, status=status.HTTP_400_BAD_REQUEST)
            user.email = email
        if "phone_number" in request.data:
            phone = (request.data.get("phone_number") or "").strip()
            if phone and User.objects.exclude(pk=user.pk).filter(phone_number=phone).exists():
                return Response({"detail": "A user with this phone number already exists."}, status=status.HTTP_400_BAD_REQUEST)
            user.phone_number = phone
            profile.phone_number = phone
        if "password" in request.data and request.data.get("password"):
            user.set_password(request.data["password"])
        if "is_active" in request.data:
            active = _as_bool(request.data.get("is_active"))
            user.is_active = active
            profile.is_active = active
        user.save()

        for field in ["title", "can_register_tenants", "can_manage_leads", "can_view_tenants"]:
            if field in request.data:
                if field == "title":
                    setattr(profile, field, (request.data.get(field) or "").strip())
                else:
                    setattr(profile, field, _as_bool(request.data.get(field)))
        profile.save()

        record_support_activity(
            request,
            "support_account_updated",
            f"Updated support account for {user.email}",
            "support_accounts",
            {"support_user_id": user.id},
        )
        return Response(SupportExecutiveSerializer(profile).data)

    def delete(self, request, user_id):
        _ensure_public()
        try:
            profile = SupportExecutiveProfile.objects.select_related("user").get(user_id=user_id)
        except SupportExecutiveProfile.DoesNotExist:
            return Response({"detail": "Support executive not found."}, status=status.HTTP_404_NOT_FOUND)

        profile.is_active = False
        profile.save(update_fields=["is_active", "updated_at"])
        profile.user.is_active = False
        profile.user.save(update_fields=["is_active", "updated_at"])
        record_support_activity(
            request,
            "support_account_deactivated",
            f"Deactivated support account for {profile.user.email}",
            "support_accounts",
            {"support_user_id": profile.user_id},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class SuperadminSupportActivityListView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        limit = min(max(int(request.query_params.get("limit", 100)), 1), 300)
        qs = SupportActivityLog.objects.select_related("support_user")
        support_user = request.query_params.get("support_user")
        if support_user:
            qs = qs.filter(support_user_id=support_user)
        action = request.query_params.get("action")
        if action:
            qs = qs.filter(action=action)

        summary = (
            qs.values("support_user__email")
            .annotate(actions=Count("id"))
            .order_by("-actions")[:10]
        )
        return Response({
            "summary": list(summary),
            "results": SupportActivityLogSerializer(qs[:limit], many=True).data,
        })


class SuperadminAccountListCreateView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        users = User.objects.filter(is_superuser=True).order_by("-is_active", "email")
        return Response({
            "active_count": User.objects.filter(is_superuser=True, is_active=True).count(),
            "results": SuperAdminAccountSerializer(users, many=True).data,
        })

    def post(self, request):
        _ensure_public()
        email = (request.data.get("email") or "").strip().lower()
        phone_number = (request.data.get("phone_number") or "").strip()
        password = request.data.get("password") or ""
        first_name = (request.data.get("first_name") or "").strip()
        last_name = (request.data.get("last_name") or "").strip()

        if not email or not phone_number or not password:
            return Response(
                {"detail": "Email, phone number, and password are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if User.objects.filter(email__iexact=email).exists():
            return Response({"detail": "A user with this email already exists."}, status=status.HTTP_400_BAD_REQUEST)
        if User.objects.filter(phone_number=phone_number).exists():
            return Response({"detail": "A user with this phone number already exists."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.create_superuser(
                email=email,
                password=password,
                phone_number=phone_number,
                first_name=first_name,
                last_name=last_name,
            )
        except IntegrityError:
            return Response(
                {"detail": "Could not create superadmin credentials. Check unique email and phone values."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        record_superadmin_activity(
            request,
            "superadmin_created",
            f"Created superadmin credentials for {email}",
            target_user=user,
            metadata={"target_user_id": user.id},
        )
        return Response(SuperAdminAccountSerializer(user).data, status=status.HTTP_201_CREATED)


class SuperadminAccountDetailView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def patch(self, request, user_id):
        _ensure_public()
        try:
            user = User.objects.get(pk=user_id, is_superuser=True)
        except User.DoesNotExist:
            return Response({"detail": "Superadmin account not found."}, status=status.HTTP_404_NOT_FOUND)

        if "is_active" in request.data:
            next_active = _as_bool(request.data.get("is_active"))
            if not next_active and _active_superadmin_count(exclude_user_id=user.id) < 1:
                return Response(
                    {"detail": "At least one active superadmin must remain."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user.is_active = next_active

        if "email" in request.data:
            email = (request.data.get("email") or "").strip().lower()
            if email and User.objects.exclude(pk=user.pk).filter(email__iexact=email).exists():
                return Response({"detail": "A user with this email already exists."}, status=status.HTTP_400_BAD_REQUEST)
            user.email = email
        if "phone_number" in request.data:
            phone = (request.data.get("phone_number") or "").strip()
            if phone and User.objects.exclude(pk=user.pk).filter(phone_number=phone).exists():
                return Response({"detail": "A user with this phone number already exists."}, status=status.HTTP_400_BAD_REQUEST)
            user.phone_number = phone
        for field in ["first_name", "last_name"]:
            if field in request.data:
                setattr(user, field, (request.data.get(field) or "").strip())
        password_changed = False
        if request.data.get("password"):
            user.set_password(request.data["password"])
            password_changed = True

        user.is_staff = True
        user.is_superuser = True
        user.role = "admin"
        user.save()
        
        # If deactivated, blacklist tokens and mirror user deactivation across tenants
        if not user.is_active:
            if OutstandingToken and BlacklistedToken:
                tokens = OutstandingToken.objects.filter(user=user)
                for token in tokens:
                    BlacklistedToken.objects.get_or_create(token=token)
            revoke_superadmin_tenant_mirrors.delay(user.email)

        record_superadmin_activity(
            request,
            "superadmin_password_changed" if password_changed else "superadmin_updated",
            f"{'Changed password for' if password_changed else 'Updated superadmin credentials for'} {user.email}",
            target_user=user,
            metadata={"target_user_id": user.id, "password_changed": password_changed},
        )
        return Response(SuperAdminAccountSerializer(user).data)

    def delete(self, request, user_id):
        _ensure_public()
        try:
            user = User.objects.get(pk=user_id, is_superuser=True)
        except User.DoesNotExist:
            return Response({"detail": "Superadmin account not found."}, status=status.HTTP_404_NOT_FOUND)

        if _active_superadmin_count(exclude_user_id=user.id) < 1:
            return Response(
                {"detail": "At least one active superadmin must remain."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        email = user.email
        target_id = user.id
        retired_email = f"deleted-superadmin-{target_id}-{uuid4().hex[:8]}@deleted.netily.local"
        retired_phone = f"+999{target_id:012d}"
        record_superadmin_activity(
            request,
            "superadmin_retired",
            f"Retired superadmin credentials for {email}",
            target_user=user,
            metadata={"target_user_id": target_id, "email": email},
        )
        user.set_unusable_password()
        user.email = retired_email
        user.phone_number = retired_phone
        user.is_active = False
        user.is_staff = False
        user.is_superuser = False
        user.role = "staff"
        user.save(update_fields=[
            "password", "email", "phone_number", "is_active",
            "is_staff", "is_superuser", "role", "updated_at",
        ])
        
        # Blacklist tokens and fan out tenant mirror revocation
        if OutstandingToken and BlacklistedToken:
            tokens = OutstandingToken.objects.filter(user=user)
            for token in tokens:
                BlacklistedToken.objects.get_or_create(token=token)
        # Use the original email we just cached before scrambling it
        revoke_superadmin_tenant_mirrors.delay(email)
        
        return Response(status=status.HTTP_204_NO_CONTENT)


class SuperadminActivityListCreateView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        limit = min(max(int(request.query_params.get("limit", 120)), 1), 500)
        qs = SuperAdminActivityLog.objects.select_related("actor", "target_user")
        actor = request.query_params.get("actor")
        target_user = request.query_params.get("target_user")
        action = request.query_params.get("action")
        if actor:
            qs = qs.filter(actor_id=actor)
        if target_user:
            qs = qs.filter(target_user_id=target_user)
        if action:
            qs = qs.filter(action=action)

        summary = qs.values("actor__email").annotate(actions=Count("id")).order_by("-actions")[:10]
        return Response({
            "summary": list(summary),
            "results": SuperAdminActivityLogSerializer(qs[:limit], many=True).data,
        })

    def post(self, request):
        _ensure_public()
        action = (request.data.get("action") or "superadmin_action").strip()[:80]
        summary = (request.data.get("summary") or "Superadmin action recorded").strip()
        metadata = request.data.get("metadata") if isinstance(request.data.get("metadata"), dict) else {}
        record_superadmin_activity(request, action, summary, metadata=metadata)
        return Response({"detail": "Superadmin activity recorded."}, status=status.HTTP_201_CREATED)
