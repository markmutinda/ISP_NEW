"""
Superadmin Views
────────────────
Platform-owner endpoints that operate in the PUBLIC schema.
Cross-schema reads use raw SQL to peek into tenant schemas.
"""

import csv
import io
import logging
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.db.models import Sum, Count, Q, F
from django.http import HttpResponse
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import Tenant, Company, Domain, User, AuditLog, GlobalSystemSettings
from .permissions import IsSuperAdmin
from .serializers import (
    TenantListSerializer,
    TenantDetailSerializer,
    TenantUpdateSerializer,
    TenantCreateSerializer,
    CompanyUpdateSerializer,
    NetilyPlanSerializer,
    UserListSerializer,
    UserDetailSerializer,
    DashboardKPISerializer,
    AuditLogSerializer,
)

logger = logging.getLogger(__name__)

SUPERADMIN_PERMS = [IsAuthenticated, IsSuperAdmin]

PAGE_SIZE = 20  # Match DRF global setting


def _ensure_public():
    """Ensure we are on the public schema."""
    connection.set_schema_to_public()


def _log_action(user, action, model_name, object_repr=None, object_id=None, changes=None, request=None):
    """Write to AuditLog for superadmin actions."""
    ip = None
    ua = None
    if request:
        ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR"))
        if ip and "," in ip:
            ip = ip.split(",")[0].strip()
        ua = request.META.get("HTTP_USER_AGENT", "")
    try:
        AuditLog.log_action(
            user=user,
            action=action,
            model_name=model_name,
            object_id=str(object_id) if object_id else None,
            object_repr=object_repr,
            changes=changes,
            ip_address=ip,
            user_agent=ua,
        )
    except Exception as e:
        logger.error("Failed to write audit log: %s", e)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DASHBOARD KPI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class DashboardView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        now = timezone.now()
        thirty_days_ago = now - timedelta(days=30)

        tenants = Tenant.objects.all()
        total_tenants = tenants.count()
        active_tenants = tenants.filter(status="active").count()
        trial_tenants = tenants.filter(status="trial").count()
        suspended_tenants = tenants.filter(status="suspended").count()

        total_users = User.objects.count()
        recent_signups = tenants.filter(created_at__gte=thirty_days_ago).count()

        # Revenue from subscriptions (public schema)
        total_revenue = Decimal("0.00")
        mrr = Decimal("0.00")
        try:
            from apps.subscriptions.models import SubscriptionPayment, CompanySubscription
            total_revenue = (
                SubscriptionPayment.objects
                .filter(status="completed")
                .aggregate(total=Sum("amount"))["total"]
                or Decimal("0.00")
            )
            mrr = (
                CompanySubscription.objects
                .filter(status="active")
                .aggregate(total=Sum("plan__price_monthly"))["total"]
                or Decimal("0.00")
            )
        except Exception:
            pass

        data = {
            "total_tenants": total_tenants,
            "active_tenants": active_tenants,
            "trial_tenants": trial_tenants,
            "suspended_tenants": suspended_tenants,
            "total_users": total_users,
            "total_revenue": total_revenue,
            "mrr": mrr,
            "recent_signups": recent_signups,
        }
        return Response(DashboardKPISerializer(data).data)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TENANTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TenantListView(ListAPIView):
    permission_classes = SUPERADMIN_PERMS
    serializer_class = TenantListSerializer
    pagination_class = None  # Return flat array — frontend expects Tenant[]

    def get_queryset(self):
        _ensure_public()
        qs = Tenant.objects.select_related("company").prefetch_related("domains").all()

        # Search
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(
                Q(subdomain__icontains=search)
                | Q(company__name__icontains=search)
                | Q(company__email__icontains=search)
            )

        # Filter by status
        tenant_status = self.request.query_params.get("status")
        if tenant_status:
            qs = qs.filter(status=tenant_status)

        # Ordering
        ordering = self.request.query_params.get("ordering", "-created_at")
        allowed = {
            "created_at", "-created_at",
            "subdomain", "-subdomain",
            "company__name", "-company__name",
            "status", "-status",
            "monthly_rate", "-monthly_rate",
        }
        if ordering in allowed:
            qs = qs.order_by(ordering)
        else:
            qs = qs.order_by("-created_at")

        return qs


class TenantCreateView(APIView):
    """Create a new tenant: Company + Tenant + Domain + Admin User."""
    permission_classes = SUPERADMIN_PERMS

    def post(self, request):
        _ensure_public()
        ser = TenantCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        # 1. Create Company
        company = Company.objects.create(
            name=d["company_name"],
            slug=slugify(d["company_name"]),
            company_type=d["company_type"],
            email=d["company_email"],
            phone_number=d["company_phone"],
            address=d.get("address", ""),
            city=d.get("city", ""),
            county=d.get("county", ""),
        )

        # 2. Create Tenant (triggers schema creation via django-tenants)
        tenant = Tenant(
            company=company,
            subdomain=d["subdomain"],
            database_name=d["subdomain"],
            status=d["status"],
            max_users=d["max_users"],
            max_customers=d["max_customers"],
            billing_cycle=d["billing_cycle"],
            monthly_rate=d["monthly_rate"],
        )
        tenant.save()  # schema_name auto-set from subdomain

        # 3. Create Domain
        Domain.objects.create(
            tenant=tenant,
            domain=f"{d['subdomain']}.localhost",
            is_primary=True,
        )

        # 4. Create admin user in public schema
        admin_user = User.objects.create_user(
            email=d["admin_email"],
            password=d["admin_password"],
            first_name=d.get("admin_first_name", "Admin"),
            last_name=d.get("admin_last_name", ""),
            phone_number=d["admin_phone"],
            role="admin",
            is_staff=True,
            is_verified=True,
            company=company,
            tenant=tenant,
            company_name=company.name,
            tenant_subdomain=tenant.subdomain,
        )

        # 5. Create trial subscription if subscriptions app exists
        try:
            from apps.subscriptions.models import CompanySubscription
            CompanySubscription.create_trial_subscription(company)
        except Exception as e:
            logger.warning("Could not create trial subscription: %s", e)

        _log_action(
            request.user, "create", "Tenant",
            object_repr=tenant.subdomain,
            object_id=tenant.id,
            changes={"company": company.name, "admin": d["admin_email"]},
            request=request,
        )

        return Response(
            TenantDetailSerializer(tenant).data,
            status=status.HTTP_201_CREATED,
        )


class TenantDetailView(RetrieveUpdateDestroyAPIView):
    permission_classes = SUPERADMIN_PERMS
    lookup_field = "pk"

    def get_queryset(self):
        _ensure_public()
        return Tenant.objects.select_related("company").prefetch_related("domains").all()

    def get_serializer_class(self):
        if self.request.method in ("PATCH", "PUT"):
            return TenantUpdateSerializer
        return TenantDetailSerializer

    def perform_update(self, serializer):
        old_status = serializer.instance.status
        instance = serializer.save()
        changes = serializer.validated_data.copy()
        # Convert Decimal to str for JSON
        for k, v in changes.items():
            if isinstance(v, Decimal):
                changes[k] = str(v)
        _log_action(
            self.request.user, "update", "Tenant",
            object_repr=instance.subdomain,
            object_id=instance.id,
            changes=changes,
            request=self.request,
        )

    def perform_destroy(self, instance):
        """Hard-delete: drop the schema, then delete Company + Tenant rows."""
        schema = instance.schema_name
        company = instance.company
        logger.warning(
            "SUPERADMIN %s is deleting tenant %s (schema=%s)",
            self.request.user.email, instance.subdomain, schema,
        )
        _log_action(
            self.request.user, "delete", "Tenant",
            object_repr=instance.subdomain,
            object_id=instance.id,
            changes={"schema": schema, "company": company.name},
            request=self.request,
        )
        try:
            from django.db import connection as db_conn
            with db_conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        except Exception as e:
            logger.error("Failed to drop schema %s: %s", schema, e)

        instance.domains.all().delete()
        instance.delete()
        company.delete()


class CompanyUpdateView(APIView):
    """PATCH company details for a tenant."""
    permission_classes = SUPERADMIN_PERMS

    def patch(self, request, pk):
        _ensure_public()
        try:
            tenant = Tenant.objects.select_related("company").get(pk=pk)
        except Tenant.DoesNotExist:
            return Response({"detail": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        ser = CompanyUpdateSerializer(tenant.company, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()

        _log_action(
            request.user, "update", "Company",
            object_repr=tenant.company.name,
            object_id=tenant.company.id,
            changes=ser.validated_data,
            request=request,
        )
        return Response(TenantDetailSerializer(tenant).data)


class TenantSuspendView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, pk):
        _ensure_public()
        try:
            tenant = Tenant.objects.get(pk=pk)
        except Tenant.DoesNotExist:
            return Response({"detail": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        reason = request.data.get("reason", "")
        tenant.status = "suspended"
        tenant.save(update_fields=["status", "updated_at"])

        _log_action(
            request.user, "update", "Tenant",
            object_repr=tenant.subdomain,
            object_id=tenant.id,
            changes={"action": "suspend", "reason": reason},
            request=request,
        )
        return Response({"detail": f"Tenant {tenant.subdomain} suspended.", "status": "suspended"})


class TenantActivateView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, pk):
        _ensure_public()
        try:
            tenant = Tenant.objects.get(pk=pk)
        except Tenant.DoesNotExist:
            return Response({"detail": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        tenant.status = "active"
        extend_days = request.data.get("extend_days")
        if extend_days:
            today = timezone.now().date()
            base = tenant.subscription_expiry if tenant.subscription_expiry and tenant.subscription_expiry > today else today
            tenant.subscription_expiry = base + timedelta(days=int(extend_days))

        tenant.save(update_fields=["status", "subscription_expiry", "updated_at"])

        _log_action(
            request.user, "update", "Tenant",
            object_repr=tenant.subdomain,
            object_id=tenant.id,
            changes={"action": "activate", "extend_days": extend_days},
            request=request,
        )
        return Response(TenantListSerializer(tenant).data)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PLANS (NetilyPlan CRUD)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class PlanListView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        try:
            from apps.subscriptions.models import NetilyPlan, CompanySubscription
            plans = NetilyPlan.objects.all().order_by("sort_order", "price_monthly")
            data = []
            for p in plans:
                d = NetilyPlanSerializer(p).data
                d["subscriber_count"] = CompanySubscription.objects.filter(plan=p).count()
                data.append(d)
            return Response(data)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request):
        _ensure_public()
        try:
            from apps.subscriptions.models import NetilyPlan
            ser = NetilyPlanSerializer(data=request.data)
            ser.is_valid(raise_exception=True)
            plan = NetilyPlan.objects.create(**ser.validated_data)
            _log_action(request.user, "create", "NetilyPlan", plan.name, plan.id, request=request)
            return Response(NetilyPlanSerializer(plan).data, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class PlanDetailView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def patch(self, request, pk):
        _ensure_public()
        try:
            from apps.subscriptions.models import NetilyPlan
            plan = NetilyPlan.objects.get(pk=pk)
        except Exception:
            return Response({"detail": "Plan not found"}, status=status.HTTP_404_NOT_FOUND)

        for field, value in request.data.items():
            if hasattr(plan, field) and field not in ("id", "created_at"):
                setattr(plan, field, value)
        plan.save()
        _log_action(request.user, "update", "NetilyPlan", plan.name, plan.id, request=request)
        return Response(NetilyPlanSerializer(plan).data)

    def delete(self, request, pk):
        _ensure_public()
        try:
            from apps.subscriptions.models import NetilyPlan
            plan = NetilyPlan.objects.get(pk=pk)
        except Exception:
            return Response({"detail": "Plan not found"}, status=status.HTTP_404_NOT_FOUND)

        name = plan.name
        plan.delete()
        _log_action(request.user, "delete", "NetilyPlan", name, pk, request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  USERS  (cross-tenant, public schema)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class UserListView(ListAPIView):
    permission_classes = SUPERADMIN_PERMS
    serializer_class = UserListSerializer

    def get_queryset(self):
        _ensure_public()
        # Exclude the requesting superadmin from the list
        qs = User.objects.exclude(pk=self.request.user.pk)

        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(
                Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(phone_number__icontains=search)
            )

        role = self.request.query_params.get("role")
        if role:
            qs = qs.filter(role=role)

        tenant = self.request.query_params.get("tenant")
        if tenant:
            qs = qs.filter(tenant_subdomain=tenant)

        ordering = self.request.query_params.get("ordering", "-date_joined")
        qs = qs.order_by(ordering)
        return qs


class UserDetailView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request, pk):
        _ensure_public()
        try:
            user = User.objects.get(pk=pk)
        except User.DoesNotExist:
            return Response({"detail": "User not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(UserDetailSerializer(user).data)

    def patch(self, request, pk):
        _ensure_public()
        try:
            user = User.objects.get(pk=pk)
        except User.DoesNotExist:
            return Response({"detail": "User not found"}, status=status.HTTP_404_NOT_FOUND)

        allowed = {"is_active", "role", "first_name", "last_name", "phone_number", "is_verified"}
        changes = {}
        for field in allowed:
            if field in request.data:
                old = getattr(user, field)
                setattr(user, field, request.data[field])
                changes[field] = {"old": str(old), "new": str(request.data[field])}
        user.save()

        _log_action(request.user, "update", "User", user.email, user.id, changes, request)
        return Response(UserDetailSerializer(user).data)


class UserDeactivateView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, pk):
        _ensure_public()
        try:
            user = User.objects.get(pk=pk)
        except User.DoesNotExist:
            return Response({"detail": "User not found"}, status=status.HTTP_404_NOT_FOUND)

        # Prevent self-deactivation
        if user.pk == request.user.pk:
            return Response(
                {"detail": "You cannot deactivate your own account."},
                status=status.HTTP_403_FORBIDDEN,
            )
        # Prevent deactivating other superadmins
        if user.is_superuser:
            return Response(
                {"detail": "Cannot deactivate a superadmin account."},
                status=status.HTTP_403_FORBIDDEN,
            )

        user.is_active = False
        user.save(update_fields=["is_active"])
        _log_action(request.user, "update", "User", user.email, user.id,
                     {"action": "deactivate"}, request)
        return Response({"detail": f"User {user.email} deactivated."})


class UserActivateView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, pk):
        _ensure_public()
        try:
            user = User.objects.get(pk=pk)
        except User.DoesNotExist:
            return Response({"detail": "User not found"}, status=status.HTTP_404_NOT_FOUND)

        user.is_active = True
        user.save(update_fields=["is_active"])
        _log_action(request.user, "update", "User", user.email, user.id,
                     {"action": "activate"}, request)
        return Response({"detail": f"User {user.email} activated."})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PAYMENTS  (public schema — subscriptions)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class PaymentListView(APIView):
    """List subscription payments across all tenants."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        try:
            from apps.subscriptions.models import SubscriptionPayment
            qs = SubscriptionPayment.objects.select_related(
                "subscription", "subscription__company", "subscription__plan"
            ).order_by("-created_at")

            # Status filter
            status_filter = request.query_params.get("status")
            if status_filter:
                qs = qs.filter(status=status_filter)

            # Search
            search = request.query_params.get("search")
            if search:
                qs = qs.filter(
                    Q(subscription__company__name__icontains=search)
                    | Q(mpesa_receipt__icontains=search)
                    | Q(payhero_reference__icontains=search)
                    | Q(bank_reference__icontains=search)
                )

            # Pagination
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("page_size", PAGE_SIZE))
            start = (page - 1) * page_size
            end = start + page_size

            total = qs.count()
            payments = qs[start:end]

            data = []
            for p in payments:
                ref = p.mpesa_receipt or p.payhero_reference or p.bank_reference or ""
                plan_name = ""
                company_name = "—"
                if p.subscription:
                    if p.subscription.company:
                        company_name = p.subscription.company.name
                    if p.subscription.plan:
                        plan_name = p.subscription.plan.name

                data.append({
                    "id": str(p.id),
                    "company_name": company_name,
                    "plan_name": plan_name,
                    "amount": str(p.amount),
                    "currency": p.currency,
                    "status": p.status,
                    "payment_method": p.payment_method or "",
                    "reference": ref,
                    "created_at": p.created_at.isoformat(),
                })

            return Response({
                "count": total,
                "page": page,
                "page_size": page_size,
                "results": data,
            })
        except Exception as e:
            logger.exception("PaymentListView error")
            return Response({"results": [], "count": 0, "page": 1, "page_size": PAGE_SIZE, "error": str(e)})


class PaymentSummaryView(APIView):
    """Revenue summary for dashboard cards."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        now = timezone.now()
        try:
            from apps.subscriptions.models import SubscriptionPayment
            completed = SubscriptionPayment.objects.filter(status="completed")
            total = completed.aggregate(t=Sum("amount"))["t"] or Decimal("0.00")
            this_month = completed.filter(
                created_at__year=now.year, created_at__month=now.month
            ).aggregate(t=Sum("amount"))["t"] or Decimal("0.00")
            last_month_start = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
            last_month_end = now.replace(day=1) - timedelta(days=1)
            last_month = completed.filter(
                created_at__date__gte=last_month_start, created_at__date__lte=last_month_end
            ).aggregate(t=Sum("amount"))["t"] or Decimal("0.00")

            return Response({
                "total_revenue": float(total),
                "this_month": float(this_month),
                "last_month": float(last_month),
                "currency": "KES",
            })
        except Exception:
            return Response({
                "total_revenue": 0,
                "this_month": 0,
                "last_month": 0,
                "currency": "KES",
            })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ANALYTICS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class RevenueTrendView(APIView):
    """Monthly revenue for last 12 months."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        now = timezone.now()
        months = int(request.query_params.get("months", 12))
        data = []

        try:
            from apps.subscriptions.models import SubscriptionPayment
            for i in range(months - 1, -1, -1):
                dt = now - timedelta(days=30 * i)
                year, month = dt.year, dt.month
                rev = (
                    SubscriptionPayment.objects
                    .filter(status="completed", created_at__year=year, created_at__month=month)
                    .aggregate(t=Sum("amount"))["t"]
                    or Decimal("0.00")
                )
                count = (
                    SubscriptionPayment.objects
                    .filter(status="completed", created_at__year=year, created_at__month=month)
                    .count()
                )
                data.append({
                    "month": f"{year}-{month:02d}",
                    "revenue": float(rev),
                    "count": count,
                })
        except Exception:
            pass

        return Response(data)


class TenantGrowthView(APIView):
    """New tenants per month over last 12 months."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        now = timezone.now()
        months = int(request.query_params.get("months", 12))
        data = []

        for i in range(months - 1, -1, -1):
            dt = now - timedelta(days=30 * i)
            year, month = dt.year, dt.month
            new = Tenant.objects.filter(
                created_at__year=year, created_at__month=month
            ).count()
            cumulative = Tenant.objects.filter(
                created_at__year__lte=year,
                created_at__month__lte=month if dt.year == year else 12,
            ).count()
            data.append({
                "month": f"{year}-{month:02d}",
                "new_tenants": new,
                "cumulative": Tenant.objects.filter(
                    created_at__lte=dt.replace(day=28)  # safe end-of-month approx
                ).count(),
            })

        return Response(data)


class ChurnView(APIView):
    """Churn metrics."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        total = Tenant.objects.count()
        active = Tenant.objects.filter(status="active").count()
        trial = Tenant.objects.filter(status="trial").count()
        suspended = Tenant.objects.filter(status="suspended").count()
        cancelled = Tenant.objects.filter(status="cancelled").count()
        churn_rate = round(((suspended + cancelled) / total * 100), 2) if total > 0 else 0

        # Trial conversion
        try:
            from apps.subscriptions.models import CompanySubscription
            total_trials = CompanySubscription.objects.filter(is_trial=True).count()
            converted = CompanySubscription.objects.filter(
                converted_from_trial_at__isnull=False
            ).count()
            # Include currently active non-trial as converted
            converted += CompanySubscription.objects.filter(
                is_trial=False, status="active"
            ).count()
            conversion_rate = round((converted / max(total_trials + converted, 1)) * 100, 1)
        except Exception:
            total_trials = trial
            converted = 0
            conversion_rate = 0

        return Response({
            "total": total,
            "active": active,
            "trial": trial,
            "suspended": suspended,
            "cancelled": cancelled,
            "churn_rate": churn_rate,
            "total_trials": total_trials,
            "converted": converted,
            "conversion_rate": conversion_rate,
        })


class PlanDistributionView(APIView):
    """Tenants/subscriptions per plan."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        try:
            from apps.subscriptions.models import NetilyPlan, CompanySubscription
            plans = NetilyPlan.objects.all().order_by("sort_order")
            data = []
            for p in plans:
                count = CompanySubscription.objects.filter(plan=p).count()
                revenue = (
                    CompanySubscription.objects
                    .filter(plan=p, status="active")
                    .aggregate(t=Sum("plan__price_monthly"))["t"]
                    or 0
                )
                data.append({
                    "plan_name": p.name,
                    "plan_code": p.code,
                    "subscriber_count": count,
                    "monthly_revenue": float(revenue),
                    "price_monthly": float(p.price_monthly),
                })
            return Response(data)
        except Exception:
            return Response([])


class TopTenantsView(APIView):
    """Top tenants by revenue or customer count."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        metric = request.query_params.get("metric", "revenue")  # revenue | customers
        limit = int(request.query_params.get("limit", 10))

        if metric == "revenue":
            tenants = (
                Tenant.objects
                .select_related("company")
                .filter(status__in=["active", "trial"])
                .order_by("-monthly_rate")[:limit]
            )
            data = [
                {
                    "id": str(t.id),
                    "subdomain": t.subdomain,
                    "company_name": t.company.name,
                    "value": float(t.monthly_rate),
                    "metric": "monthly_rate",
                    "status": t.status,
                }
                for t in tenants
            ]
        else:
            # Customers — peek into each schema
            tenants = Tenant.objects.select_related("company").filter(
                status__in=["active", "trial"]
            )
            scored = []
            for t in tenants:
                try:
                    with connection.cursor() as cur:
                        cur.execute(
                            f'SELECT COUNT(*) FROM "{t.schema_name}"."customers_customer"'
                        )
                        count = cur.fetchone()[0]
                except Exception:
                    count = 0
                scored.append({
                    "id": str(t.id),
                    "subdomain": t.subdomain,
                    "company_name": t.company.name,
                    "value": count,
                    "metric": "customers",
                    "status": t.status,
                })
            scored.sort(key=lambda x: x["value"], reverse=True)
            data = scored[:limit]

        return Response(data)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PER-TENANT STATS (cross-schema peek)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TenantStatsView(APIView):
    """Comprehensive cross-schema peek into a tenant's data."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request, pk):
        _ensure_public()
        try:
            tenant = Tenant.objects.select_related("company").get(pk=pk)
        except Tenant.DoesNotExist:
            return Response({"detail": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        schema = tenant.schema_name
        result = {
            "tenant_id": str(tenant.id),
            "subdomain": tenant.subdomain,
            "company_name": tenant.company.name,
        }

        # ── Simple counts ──
        count_tables = {
            "customers": "customers_customer",
            "routers": "network_router",
            "invoices": "billing_invoice",
            "payments": "billing_payment",
            "tickets": "support_supportticket",
            "staff": "staff_employee",
            "pppoe_users": "network_pppoeuser",
            "hotspot_users": "network_hotspotuser",
            "ip_addresses": "network_ipaddress",
            "subnets": "network_subnet",
            "equipment_items": "inventory_equipmentitem",
            "plans": "billing_plan",
            "bandwidth_profiles": "bandwidth_bandwidthprofile",
            "olt_devices": "network_oltdevice",
            "support_tickets_open": None,  # handled separately
        }
        for label, table in count_tables.items():
            if table is None:
                continue
            try:
                with connection.cursor() as cur:
                    cur.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
                    result[label] = cur.fetchone()[0]
            except Exception:
                result[label] = 0

        # ── Open tickets ──
        try:
            with connection.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM \"{schema}\".\"support_supportticket\" "
                    f"WHERE status IN ('open', 'in_progress', 'pending')"
                )
                result["support_tickets_open"] = cur.fetchone()[0]
        except Exception:
            result["support_tickets_open"] = 0

        # ── Online / offline routers ──
        try:
            with connection.cursor() as cur:
                cur.execute(f"SELECT status, COUNT(*) FROM \"{schema}\".\"network_router\" GROUP BY status")
                router_status = {}
                for row in cur.fetchall():
                    router_status[row[0]] = row[1]
                result["routers_online"] = router_status.get("online", 0)
                result["routers_offline"] = router_status.get("offline", 0)
        except Exception:
            result["routers_online"] = 0
            result["routers_offline"] = 0

        # ── PPPoE / Hotspot active users ──
        try:
            with connection.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM \"{schema}\".\"network_pppoeuser\" WHERE status = 'active'")
                result["pppoe_active"] = cur.fetchone()[0]
        except Exception:
            result["pppoe_active"] = 0

        try:
            with connection.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM \"{schema}\".\"network_hotspotuser\" WHERE status = 'active'")
                result["hotspot_active"] = cur.fetchone()[0]
        except Exception:
            result["hotspot_active"] = 0

        # ── Revenue summary (tenant-side payments) ──
        try:
            with connection.cursor() as cur:
                cur.execute(
                    f"SELECT COALESCE(SUM(amount), 0) FROM \"{schema}\".\"billing_payment\" WHERE status = 'completed'"
                )
                result["tenant_revenue"] = float(cur.fetchone()[0])
        except Exception:
            result["tenant_revenue"] = 0

        # ── Customer status breakdown ──
        try:
            with connection.cursor() as cur:
                cur.execute(f"SELECT status, COUNT(*) FROM \"{schema}\".\"customers_customer\" GROUP BY status")
                customer_status = {}
                for row in cur.fetchall():
                    customer_status[row[0]] = row[1]
                result["customers_active"] = customer_status.get("ACTIVE", 0)
                result["customers_suspended"] = customer_status.get("SUSPENDED", 0)
                result["customers_pending"] = customer_status.get("PENDING", 0)
        except Exception:
            result["customers_active"] = 0
            result["customers_suspended"] = 0
            result["customers_pending"] = 0

        # ── Equipment status breakdown ──
        try:
            with connection.cursor() as cur:
                cur.execute(f"SELECT status, COUNT(*) FROM \"{schema}\".\"inventory_equipmentitem\" GROUP BY status")
                equip_status = {}
                for row in cur.fetchall():
                    equip_status[row[0]] = row[1]
                result["equipment_in_stock"] = equip_status.get("in_stock", 0)
                result["equipment_in_use"] = equip_status.get("in_use", 0)
                result["equipment_faulty"] = equip_status.get("faulty", 0)
        except Exception:
            result["equipment_in_stock"] = 0
            result["equipment_in_use"] = 0
            result["equipment_faulty"] = 0

        return Response(result)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  AUDIT LOG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class AuditLogView(APIView):
    """Real audit log from AuditLog model."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", PAGE_SIZE))

        qs = AuditLog.objects.select_related("user").order_by("-timestamp")

        # Filters
        action = request.query_params.get("action")
        if action:
            qs = qs.filter(action=action)

        model = request.query_params.get("model")
        if model:
            qs = qs.filter(model_name__icontains=model)

        search = request.query_params.get("search")
        if search:
            qs = qs.filter(
                Q(user__email__icontains=search)
                | Q(object_repr__icontains=search)
                | Q(model_name__icontains=search)
            )

        total = qs.count()
        start = (page - 1) * page_size
        logs = qs[start:start + page_size]

        data = []
        for log in logs:
            data.append({
                "id": str(log.id),
                "timestamp": log.timestamp.isoformat(),
                "actor_email": log.user.email if log.user else "System",
                "action": log.action,
                "model_name": log.model_name,
                "object_repr": log.object_repr,
                "ip_address": log.ip_address,
                "changes": log.changes,
            })

        return Response({
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": data,
        })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ACTIVITY (recent logins, tenant changes)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ActivityView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        limit = int(request.query_params.get("limit", 30))

        # Recent logins
        recent_logins = (
            User.objects
            .filter(last_login__isnull=False)
            .order_by("-last_login")[:limit]
            .values("email", "last_login", "role", "tenant_subdomain")
        )

        # Recent tenants
        recent_tenants = (
            Tenant.objects
            .select_related("company")
            .order_by("-created_at")[:limit]
        )

        activity = []
        for u in recent_logins:
            activity.append({
                "type": "login",
                "timestamp": u["last_login"].isoformat() if u["last_login"] else None,
                "actor": u["email"],
                "detail": f"Logged in ({u['role']})",
                "target": u["tenant_subdomain"] or "—",
                "tenant": u["tenant_subdomain"] or "—",
            })
        for t in recent_tenants:
            activity.append({
                "type": "tenant_created",
                "timestamp": t.created_at.isoformat() if t.created_at else None,
                "actor": t.company.email,
                "detail": f"Tenant '{t.subdomain}' created ({t.status})",
                "target": t.subdomain,
                "tenant": t.subdomain,
            })

        activity.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
        return Response(activity[:limit])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SETTINGS (GlobalSystemSettings)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class SettingsView(APIView):
    """Platform-level settings (automation, notifications, billing).
    RADIUS config is tenant-level and not exposed here."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        settings_obj = GlobalSystemSettings.get_solo()
        # Platform-relevant fields only (no RADIUS — that's per-tenant)
        fields = [
            # Automation
            "auto_renew", "auto_expiry", "auto_notifications",
            "auto_backup", "auto_reports", "grace_period",
            "backup_frequency", "report_frequency",
            # Notifications
            "email_enabled", "sms_enabled", "payment_notifications",
            "expiry_notifications", "system_alerts", "marketing_emails",
            "admin_email", "sms_gateway",
        ]
        data = {}
        for f in fields:
            data[f] = getattr(settings_obj, f, None)
        return Response(data)

    def patch(self, request):
        _ensure_public()
        settings_obj = GlobalSystemSettings.get_solo()
        # Only allow platform-relevant fields (block RADIUS fields)
        blocked = {
            "id", "primary_server", "primary_port", "primary_secret",
            "secondary_server", "secondary_port", "secondary_secret",
            "accounting_port", "timeout", "retries",
        }
        changes = {}
        for key, value in request.data.items():
            if key in blocked:
                continue
            if hasattr(settings_obj, key) and key != "id":
                old = getattr(settings_obj, key)
                setattr(settings_obj, key, value)
                changes[key] = {"old": str(old), "new": str(value)}
        settings_obj.save()

        _log_action(request.user, "update", "GlobalSystemSettings",
                     "Global Settings", 1, changes, request)
        return Response({"detail": "Settings updated"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  EXPORT (CSV downloads)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ExportTenantsView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        tenants = Tenant.objects.select_related("company").order_by("-created_at")

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "Subdomain", "Company", "Email", "Phone", "Status",
            "Plan", "MRR", "Billing Cycle", "Expiry", "Max Users",
            "Max Customers", "Created",
        ])
        for t in tenants:
            writer.writerow([
                t.subdomain, t.company.name, t.company.email, t.company.phone_number,
                t.status, t.company.subscription_plan, t.monthly_rate,
                t.billing_cycle, t.subscription_expiry, t.max_users,
                t.max_customers, t.created_at.strftime("%Y-%m-%d"),
            ])

        _log_action(request.user, "export", "Tenant", "CSV Export", request=request)

        resp = HttpResponse(buf.getvalue(), content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="tenants_export.csv"'
        return resp


class ExportUsersView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        users = User.objects.all().order_by("-date_joined")

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "Email", "First Name", "Last Name", "Phone", "Role",
            "Active", "Verified", "Company", "Tenant", "Joined",
        ])
        for u in users:
            writer.writerow([
                u.email, u.first_name, u.last_name, u.phone_number,
                u.role, u.is_active, u.is_verified,
                u.company_name, u.tenant_subdomain,
                u.date_joined.strftime("%Y-%m-%d"),
            ])

        _log_action(request.user, "export", "User", "CSV Export", request=request)

        resp = HttpResponse(buf.getvalue(), content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="users_export.csv"'
        return resp


class ExportPaymentsView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        try:
            from apps.subscriptions.models import SubscriptionPayment
            payments = SubscriptionPayment.objects.select_related(
                "subscription__company", "subscription__plan"
            ).order_by("-created_at")

            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                "Reference", "Company", "Plan", "Amount", "Currency",
                "Method", "Status", "Date",
            ])
            for p in payments:
                ref = p.mpesa_receipt or p.payhero_reference or p.bank_reference or ""
                writer.writerow([
                    ref,
                    p.subscription.company.name if p.subscription and p.subscription.company else "",
                    p.subscription.plan.name if p.subscription and p.subscription.plan else "",
                    p.amount, p.currency, p.payment_method,
                    p.status, p.created_at.strftime("%Y-%m-%d %H:%M"),
                ])

            _log_action(request.user, "export", "SubscriptionPayment", "CSV Export", request=request)

            resp = HttpResponse(buf.getvalue(), content_type="text/csv")
            resp["Content-Disposition"] = 'attachment; filename="payments_export.csv"'
            return resp
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PER-TENANT AUDIT LOG  (cross-schema peek)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TenantAuditLogView(APIView):
    """Audit log entries for a specific tenant's schema."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request, pk):
        _ensure_public()
        try:
            tenant = Tenant.objects.get(pk=pk)
        except Tenant.DoesNotExist:
            return Response({"detail": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        schema = tenant.schema_name
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", PAGE_SIZE))
        search = request.query_params.get("search", "")
        action_filter = request.query_params.get("action", "")

        offset = (page - 1) * page_size
        where_clauses = []
        params = []

        if search:
            where_clauses.append(
                "(al.object_repr ILIKE %s OR al.model_name ILIKE %s OR u.email ILIKE %s)"
            )
            s = f"%{search}%"
            params.extend([s, s, s])

        if action_filter:
            where_clauses.append("al.action = %s")
            params.append(action_filter)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        try:
            with connection.cursor() as cur:
                # Count
                cur.execute(
                    f'SELECT COUNT(*) FROM "{schema}"."core_auditlog" al '
                    f'LEFT JOIN "{schema}"."core_user" u ON al.user_id = u.id '
                    f'{where_sql}',
                    params,
                )
                total = cur.fetchone()[0]

                # Data
                cur.execute(
                    f'SELECT al.id, al.timestamp, u.email, al.action, al.model_name, '
                    f'al.object_repr, al.ip_address, al.changes '
                    f'FROM "{schema}"."core_auditlog" al '
                    f'LEFT JOIN "{schema}"."core_user" u ON al.user_id = u.id '
                    f'{where_sql} '
                    f'ORDER BY al.timestamp DESC LIMIT %s OFFSET %s',
                    params + [page_size, offset],
                )
                rows = cur.fetchall()

            data = []
            for row in rows:
                data.append({
                    "id": str(row[0]),
                    "timestamp": row[1].isoformat() if row[1] else None,
                    "actor_email": row[2] or "System",
                    "action": row[3],
                    "model_name": row[4],
                    "object_repr": row[5],
                    "ip_address": row[6],
                    "changes": row[7],
                })

            return Response({
                "count": total,
                "page": page,
                "page_size": page_size,
                "results": data,
            })
        except Exception as e:
            logger.exception("TenantAuditLogView error for %s", schema)
            return Response({
                "count": 0, "page": 1, "page_size": page_size, "results": [],
                "error": str(e),
            })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PER-TENANT DETAILED DATA (cross-schema peek)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TenantRoutersView(APIView):
    """List routers inside a tenant's schema."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request, pk):
        _ensure_public()
        try:
            tenant = Tenant.objects.get(pk=pk)
        except Tenant.DoesNotExist:
            return Response({"detail": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        schema = tenant.schema_name
        try:
            with connection.cursor() as cur:
                cur.execute(
                    f'SELECT id, name, ip_address, router_type, config_type, status, '
                    f'total_users, active_users, uptime, last_seen, location, '
                    f'enable_hotspot, enable_pppoe, routeros_version, model '
                    f'FROM "{schema}"."network_router" ORDER BY name'
                )
                columns = [
                    "id", "name", "ip_address", "router_type", "config_type",
                    "status", "total_users", "active_users", "uptime", "last_seen",
                    "location", "enable_hotspot", "enable_pppoe",
                    "routeros_version", "model",
                ]
                rows = cur.fetchall()

            data = []
            for row in rows:
                entry = {columns[i]: row[i] for i in range(len(columns))}
                entry["id"] = str(entry["id"])
                if entry.get("last_seen"):
                    entry["last_seen"] = entry["last_seen"].isoformat()
                data.append(entry)

            return Response(data)
        except Exception as e:
            logger.exception("TenantRoutersView error")
            return Response([])


class TenantPPPoEUsersView(APIView):
    """List PPPoE users inside a tenant's schema."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request, pk):
        _ensure_public()
        try:
            tenant = Tenant.objects.get(pk=pk)
        except Tenant.DoesNotExist:
            return Response({"detail": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        schema = tenant.schema_name
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", PAGE_SIZE))
        search = request.query_params.get("search", "")
        offset = (page - 1) * page_size

        where_sql = ""
        params = []
        if search:
            where_sql = "WHERE p.username ILIKE %s OR p.caller_id ILIKE %s OR p.remote_address ILIKE %s"
            s = f"%{search}%"
            params = [s, s, s]

        try:
            with connection.cursor() as cur:
                cur.execute(
                    f'SELECT COUNT(*) FROM "{schema}"."network_pppoeuser" p {where_sql}',
                    params,
                )
                total = cur.fetchone()[0]

                cur.execute(
                    f'SELECT p.id, p.username, p.caller_id, p.local_address, '
                    f'p.remote_address, p.bytes_in, p.bytes_out, p.status, '
                    f'p.profile, p.connected_since, p.last_seen, r.name as router_name '
                    f'FROM "{schema}"."network_pppoeuser" p '
                    f'LEFT JOIN "{schema}"."network_router" r ON p.router_id = r.id '
                    f'{where_sql} '
                    f'ORDER BY p.username LIMIT %s OFFSET %s',
                    params + [page_size, offset],
                )
                columns = [
                    "id", "username", "caller_id", "local_address",
                    "remote_address", "bytes_in", "bytes_out", "status",
                    "profile", "connected_since", "last_seen", "router_name",
                ]
                rows = cur.fetchall()

            data = []
            for row in rows:
                entry = {columns[i]: row[i] for i in range(len(columns))}
                entry["id"] = str(entry["id"])
                for dt_field in ("connected_since", "last_seen"):
                    if entry.get(dt_field):
                        entry[dt_field] = entry[dt_field].isoformat()
                data.append(entry)

            return Response({"count": total, "page": page, "page_size": page_size, "results": data})
        except Exception as e:
            logger.exception("TenantPPPoEUsersView error")
            return Response({"count": 0, "page": page, "page_size": page_size, "results": []})


class TenantHotspotUsersView(APIView):
    """List hotspot users inside a tenant's schema."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request, pk):
        _ensure_public()
        try:
            tenant = Tenant.objects.get(pk=pk)
        except Tenant.DoesNotExist:
            return Response({"detail": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        schema = tenant.schema_name
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", PAGE_SIZE))
        search = request.query_params.get("search", "")
        offset = (page - 1) * page_size

        where_sql = ""
        params = []
        if search:
            where_sql = "WHERE h.username ILIKE %s OR h.mac_address ILIKE %s OR h.ip_address ILIKE %s"
            s = f"%{search}%"
            params = [s, s, s]

        try:
            with connection.cursor() as cur:
                cur.execute(
                    f'SELECT COUNT(*) FROM "{schema}"."network_hotspotuser" h {where_sql}',
                    params,
                )
                total = cur.fetchone()[0]

                cur.execute(
                    f'SELECT h.id, h.username, h.mac_address, h.ip_address, '
                    f'h.bytes_in, h.bytes_out, h.status, h.profile, '
                    f'h.connected_since, h.last_seen, r.name as router_name '
                    f'FROM "{schema}"."network_hotspotuser" h '
                    f'LEFT JOIN "{schema}"."network_router" r ON h.router_id = r.id '
                    f'{where_sql} '
                    f'ORDER BY h.username LIMIT %s OFFSET %s',
                    params + [page_size, offset],
                )
                columns = [
                    "id", "username", "mac_address", "ip_address",
                    "bytes_in", "bytes_out", "status", "profile",
                    "connected_since", "last_seen", "router_name",
                ]
                rows = cur.fetchall()

            data = []
            for row in rows:
                entry = {columns[i]: row[i] for i in range(len(columns))}
                entry["id"] = str(entry["id"])
                for dt_field in ("connected_since", "last_seen"):
                    if entry.get(dt_field):
                        entry[dt_field] = entry[dt_field].isoformat()
                data.append(entry)

            return Response({"count": total, "page": page, "page_size": page_size, "results": data})
        except Exception as e:
            logger.exception("TenantHotspotUsersView error")
            return Response({"count": 0, "page": page, "page_size": page_size, "results": []})


class TenantInventoryView(APIView):
    """List inventory items inside a tenant's schema."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request, pk):
        _ensure_public()
        try:
            tenant = Tenant.objects.get(pk=pk)
        except Tenant.DoesNotExist:
            return Response({"detail": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        schema = tenant.schema_name
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", PAGE_SIZE))
        offset = (page - 1) * page_size

        try:
            with connection.cursor() as cur:
                cur.execute(f'SELECT COUNT(*) FROM "{schema}"."inventory_equipmentitem"')
                total = cur.fetchone()[0]

                cur.execute(
                    f'SELECT e.id, e.name, e.model, e.serial_number, e.asset_tag, '
                    f'e.status, e.condition, e.location, e.mac_address, e.ip_address, '
                    f'e.purchase_price, e.warranty_expiry, t.name as type_name '
                    f'FROM "{schema}"."inventory_equipmentitem" e '
                    f'LEFT JOIN "{schema}"."inventory_equipmenttype" t ON e.equipment_type_id = t.id '
                    f'ORDER BY e.name LIMIT %s OFFSET %s',
                    [page_size, offset],
                )
                columns = [
                    "id", "name", "model", "serial_number", "asset_tag",
                    "status", "condition", "location", "mac_address", "ip_address",
                    "purchase_price", "warranty_expiry", "type_name",
                ]
                rows = cur.fetchall()

            data = []
            for row in rows:
                entry = {columns[i]: row[i] for i in range(len(columns))}
                entry["id"] = str(entry["id"])
                if entry.get("warranty_expiry"):
                    entry["warranty_expiry"] = entry["warranty_expiry"].isoformat()
                if entry.get("purchase_price"):
                    entry["purchase_price"] = float(entry["purchase_price"])
                data.append(entry)

            return Response({"count": total, "page": page, "page_size": page_size, "results": data})
        except Exception as e:
            logger.exception("TenantInventoryView error")
            return Response({"count": 0, "page": page, "page_size": page_size, "results": []})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TENANT IMPERSONATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TenantImpersonateView(APIView):
    """Generate a JWT for the tenant's admin user so the superadmin
    can access that tenant's panel without knowing the password."""
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, pk):
        _ensure_public()
        try:
            tenant = Tenant.objects.select_related("company").get(pk=pk)
        except Tenant.DoesNotExist:
            return Response({"detail": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        # Find the tenant's admin user
        admin_user = (
            User.objects
            .filter(tenant=tenant, role="admin", is_active=True)
            .order_by("date_joined")
            .first()
        )
        if not admin_user:
            return Response(
                {"detail": "No active admin user found for this tenant."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Generate JWT for that user
        try:
            from rest_framework_simplejwt.tokens import RefreshToken
            refresh = RefreshToken.for_user(admin_user)
            tokens = {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            }
        except Exception as e:
            return Response({"detail": f"Token generation failed: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Determine the tenant panel URL
        domain = tenant.domains.filter(is_primary=True).first()
        panel_url = f"http://{domain.domain}:3000/admin" if domain else f"http://{tenant.subdomain}.localhost:3000/admin"

        _log_action(
            request.user, "login", "Tenant",
            object_repr=f"Impersonated {tenant.subdomain} as {admin_user.email}",
            object_id=tenant.id,
            changes={"impersonated_user": admin_user.email},
            request=request,
        )

        return Response({
            "access": tokens["access"],
            "refresh": tokens["refresh"],
            "user": {
                "id": admin_user.id,
                "email": admin_user.email,
                "first_name": admin_user.first_name,
                "last_name": admin_user.last_name,
                "role": admin_user.role,
            },
            "tenant": {
                "subdomain": tenant.subdomain,
                "company_name": tenant.company.name,
            },
            "panel_url": panel_url,
        })
