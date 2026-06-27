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

import requests as _requests  # For external API calls
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management import call_command
from django.core.validators import validate_email
from django.db import connection, transaction
from django.db.models import Sum, Count, Q, F
from django.http import HttpResponse
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status
from rest_framework.generics import ListAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import Tenant, Company, Domain, User, AuditLog, GlobalSystemSettings, Changelog, FeatureRequest
from .permissions import IsSuperAdmin
from .models import TenantDeletionJob
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
from apps.core.serializers import ChangelogSerializer, FeatureRequestSerializer
from django_tenants.utils import schema_context, get_public_schema_name
from django.shortcuts import get_object_or_404
from .tasks import queue_changelog_notifications, queue_tenant_deletion

# ── NEW: SMS models for cross-schema overview ──
from apps.messaging.models import SMSGatewayConfig, TenantSMSWallet, SMSUnitTopup

logger = logging.getLogger(__name__)

SUPERADMIN_PERMS = [IsAuthenticated, IsSuperAdmin]

PAGE_SIZE = 20  # Match DRF global setting

# ── CRITICAL SAFETY GUARD: Protected schemas that must never be deleted ──
PROTECTED_SCHEMAS = {'public', 'information_schema', 'pg_catalog', 'pg_toast'}


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


def _serialize_deletion_job(job: TenantDeletionJob) -> dict:
    return {
        "id": str(job.id),
        "tenant_id": str(job.tenant_id) if job.tenant_id else None,
        "company_name": job.company_name,
        "subdomain": job.subdomain,
        "schema_name": job.schema_name,
        "status": job.status,
        "current_step": job.current_step,
        "progress_percent": job.progress_percent,
        "status_message": job.status_message,
        "error_message": job.error_message,
        "requested_options": job.requested_options,
        "cleanup_summary": job.cleanup_summary,
        "step_history": job.step_history,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  DASHBOARD KPI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class DashboardView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        now = timezone.now()
        thirty_days_ago = now - timedelta(days=30)

        tenants = Tenant.objects.exclude(schema_name__in=PROTECTED_SCHEMAS)
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

        # ── Cross-tenant aggregates ──
        total_pppoe = 0
        total_hotspot = 0
        total_customers = 0
        total_tenant_revenue = Decimal("0.00")
        for tenant in tenants.filter(status__in=["active", "trial"]):
            schema = tenant.schema_name
            try:
                with connection.cursor() as cur:
                    cur.execute(f'SET search_path TO "{schema}"')
                    cur.execute('SELECT COUNT(*) FROM network_pppoeuser')
                    total_pppoe += cur.fetchone()[0]
                    cur.execute('SELECT COUNT(*) FROM network_hotspotuser')
                    total_hotspot += cur.fetchone()[0]
                    cur.execute('SELECT COUNT(*) FROM customers_customer')
                    total_customers += cur.fetchone()[0]
                    cur.execute(
                        "SELECT COALESCE(SUM(amount), 0) FROM billing_payment "
                        "WHERE status = 'COMPLETED'"
                    )
                    total_tenant_revenue += Decimal(str(cur.fetchone()[0]))
            except Exception as exc:
                logger.warning("Dashboard cross-tenant error for %s: %s", schema, exc)

        # Reset to public schema after cross-tenant loop
        with connection.cursor() as cur:
            cur.execute('SET search_path TO "public"')

        data["total_pppoe_users"] = total_pppoe
        data["total_hotspot_users"] = total_hotspot
        data["total_customers"] = total_customers
        data["total_tenant_revenue"] = float(total_tenant_revenue)

        return Response(data)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TENANTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TenantListView(ListAPIView):
    permission_classes = SUPERADMIN_PERMS
    serializer_class = TenantListSerializer
    pagination_class = None  # Return flat array — frontend expects Tenant[]

    def get_queryset(self):
        _ensure_public()
        # Exclude protected schemas from the list
        qs = Tenant.objects.select_related("company").prefetch_related("domains").exclude(
            schema_name__in=PROTECTED_SCHEMAS
        ).all()

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

        schema_name = f"tenant_{d['subdomain'].replace('-', '_')}"
        if Tenant.objects.filter(schema_name=schema_name).exists():
            return Response({"detail": f"Schema {schema_name} already exists."}, status=400)

        # 2. Create Tenant
        tenant = Tenant(
            company=company,
            subdomain=d["subdomain"],
            schema_name=schema_name,
            database_name=d["subdomain"],
            status=d["status"],
            max_users=d["max_users"],
            max_customers=d["max_customers"],
            billing_cycle=d["billing_cycle"],
            monthly_rate=d["monthly_rate"],
        )
        tenant.save()
        try:
            with connection.cursor() as cur:
                cur.execute(f'CREATE SCHEMA "{tenant.schema_name}"')
            call_command("migrate_schemas_resilient", schema=tenant.schema_name)
            from apps.radius.services.tenant_radius_service import tenant_radius_service

            tenant_radius_service.configure_tenant_radius(
                schema_name=tenant.schema_name,
                tenant_name=company.name,
            )
        except Exception:
            with connection.cursor() as cur:
                cur.execute('SET search_path TO "public"')
                cur.execute(f'DROP SCHEMA IF EXISTS "{tenant.schema_name}" CASCADE')
            tenant.delete()
            company.delete()
            raise

        # 3. Create Domain (use TENANT_BASE_DOMAIN from settings)
        from django.conf import settings as conf
        base_domain = getattr(conf, 'TENANT_BASE_DOMAIN', 'localhost')
        Domain.objects.create(
            tenant=tenant,
            domain=f"{d['subdomain']}.{base_domain}",
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

            # Fire trial welcome email in background
            try:
                from apps.subscriptions.tasks import send_trial_welcome_email
                send_trial_welcome_email.delay(company.id)
            except Exception as email_err:
                logger.warning("Could not queue trial welcome email: %s", email_err)
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
        # Exclude protected schemas from being accessed/deleted
        return Tenant.objects.select_related("company").prefetch_related("domains").exclude(
            schema_name__in=PROTECTED_SCHEMAS
        ).all()

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
        from django.db import connection as db_conn, transaction
        from rest_framework.exceptions import PermissionDenied

        schema = instance.schema_name
        company = instance.company

        # ── CRITICAL SAFETY GUARD ──────────────────────────────────────────
        if schema in PROTECTED_SCHEMAS:
            raise PermissionDenied(
                f"Schema '{schema}' is protected and cannot be deleted. "
                f"This is a critical system schema."
            )
        # ──────────────────────────────────────────────────────────────────

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

        # Step 1: Make sure we're on public schema before any ORM work
        connection.set_schema_to_public()

        # Step 2: Drop the tenant schema using raw SQL with autocommit isolation
        try:
            with connection.cursor() as cur:
                # Ensure we're on public first
                cur.execute('SET search_path TO "public"')
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                logger.info("Dropped schema: %s", schema)
        except Exception as e:
            logger.error("Failed to drop schema %s: %s", schema, e)
            # Don't re-raise — continue cleanup

        # Step 3: Delete ORM records (domains → tenant → company) in public schema
        connection.set_schema_to_public()
        with transaction.atomic():
            try:
                instance.domains.all().delete()
            except Exception as e:
                logger.warning("Could not delete domains for %s: %s", schema, e)
            try:
                # Delete the tenant by PK directly to avoid any schema confusion
                from apps.core.models import Tenant as TenantModel
                TenantModel.objects.filter(pk=instance.pk).delete()
            except Exception as e:
                logger.warning("Could not delete tenant %s: %s", schema, e)
            try:
                from apps.core.models import Company as CompanyModel
                CompanyModel.objects.filter(pk=company.pk).delete()
            except Exception as e:
                logger.warning("Could not delete company for %s: %s", schema, e)


    def destroy(self, request, *args, **kwargs):
        return Response(
            {
                "detail": "Permanent deletion now runs as a tracked background job. "
                "Use the tenant delete-request endpoint instead."
            },
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )


class TenantDeletionRequestView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, pk):
        _ensure_public()
        tenant = get_object_or_404(
            Tenant.objects.select_related("company").exclude(schema_name__in=PROTECTED_SCHEMAS),
            pk=pk,
        )

        confirmation_name = str(request.data.get("confirmation_name", "")).strip()
        allowed_confirmations = {tenant.company.name.strip().lower(), tenant.subdomain.strip().lower()}
        if confirmation_name.lower() not in allowed_confirmations:
            return Response(
                {
                    "detail": "Confirmation text does not match the tenant name.",
                    "expected": tenant.company.name,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        active_job = (
            TenantDeletionJob.objects.filter(
                tenant=tenant,
                status__in=[TenantDeletionJob.STATUS_QUEUED, TenantDeletionJob.STATUS_RUNNING],
            )
            .order_by("-created_at")
            .first()
        )
        if active_job:
            return Response(_serialize_deletion_job(active_job), status=status.HTTP_202_ACCEPTED)

        job = TenantDeletionJob.objects.create(
            tenant=tenant,
            requested_by=request.user,
            company_name=tenant.company.name,
            subdomain=tenant.subdomain,
            schema_name=tenant.schema_name,
            status=TenantDeletionJob.STATUS_QUEUED,
            current_step=TenantDeletionJob.STEP_QUEUED,
            progress_percent=0,
            status_message="Deletion queued. Access revocation will start shortly.",
            requested_options={
                "cleanup_media": True,
                "cleanup_integrations": True,
                "revoke_access_immediately": True,
            },
        )

        _log_action(
            request.user,
            "delete",
            "TenantDeletionJob",
            object_repr=tenant.subdomain,
            object_id=job.id,
            changes={
                "tenant_id": str(tenant.id),
                "schema_name": tenant.schema_name,
                "status": "queued",
            },
            request=request,
        )
        queue_tenant_deletion(str(job.id))
        return Response(_serialize_deletion_job(job), status=status.HTTP_202_ACCEPTED)


class TenantDeletionJobDetailView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request, job_id):
        _ensure_public()
        job = get_object_or_404(TenantDeletionJob.objects.select_related("tenant"), pk=job_id)
        return Response(_serialize_deletion_job(job))


class HardDeleteTenantView(APIView):
    """
    Synchronous hard-delete escape hatch.

    DELETE /api/v1/superadmin/tenants/<pk>/hard-delete/
    Body: { "confirmation_name": "<company name or subdomain>" }

    Bypasses Celery and calls purge_tenant_completely() directly.
    Use when the async deletion job failed and left ghost records,
    or when you need a guaranteed, immediate, zero-ghost-record deletion.

    Deletion order (FK-safe):
      SubscriptionPayment -> CompanySubscription -> User -> Domain
      -> Tenant -> Company -> DROP SCHEMA CASCADE (outside transaction)
    """
    permission_classes = SUPERADMIN_PERMS

    def delete(self, request, pk):
        _ensure_public()
        tenant = get_object_or_404(
            Tenant.objects.select_related("company").exclude(schema_name__in=PROTECTED_SCHEMAS),
            pk=pk,
        )

        # Confirmation gate
        confirmation_name = str(request.data.get("confirmation_name", "")).strip()
        company_name_val = tenant.company.name if tenant.company else ""
        expected = {tenant.subdomain.strip().lower(), company_name_val.strip().lower()}
        if confirmation_name.lower() not in expected:
            return Response(
                {
                    "detail": "Confirmation text does not match the tenant name or subdomain.",
                    "expected": company_name_val or tenant.subdomain,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Only superusers may hard-delete
        if not request.user.is_superuser:
            return Response(
                {"detail": "Only superusers may perform a hard delete."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Snapshot identifiers before the purge removes the rows
        tenant_id = str(tenant.pk)
        subdomain = tenant.subdomain
        schema_name = tenant.schema_name

        logger.warning(
            "SUPERADMIN %s initiating HARD DELETE -- tenant=%s schema=%s",
            request.user.email, subdomain, schema_name,
        )

        # Execute purge
        try:
            from apps.core.tenant_purge import purge_tenant_completely
            result = purge_tenant_completely(pk)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Hard delete failed for tenant %s", subdomain)
            return Response(
                {"detail": f"Hard delete failed: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Cancel any queued/running deletion jobs for this tenant
        TenantDeletionJob.objects.filter(
            schema_name=schema_name,
            status__in=[TenantDeletionJob.STATUS_QUEUED, TenantDeletionJob.STATUS_RUNNING],
        ).update(
            status=TenantDeletionJob.STATUS_COMPLETED,
            status_message="Superseded by synchronous hard-delete.",
        )

        # Audit log
        _log_action(
            request.user, "delete", "Tenant",
            object_repr=subdomain,
            object_id=tenant_id,
            changes={
                "method": "hard_delete",
                "schema_name": schema_name,
                "company_name": company_name_val,
                "purge_summary": result.as_dict(),
            },
            request=request,
        )

        return Response(
            {
                "detail": (
                    f"Tenant '{company_name_val}' ({subdomain}) has been permanently deleted."
                ),
                "purge_summary": result.as_dict(),
            },
            status=status.HTTP_200_OK,
        )

    # Backwards-compatible for any older frontend bundle that still posts here.
    def post(self, request, pk):
        return self.delete(request, pk)


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
    """
    POST /api/v1/superadmin/tenants/<pk>/suspend/

    Suspends the tenant and syncs CompanySubscription.status → 'suspended'
    so the serializer (which derives the frontend status from CompanySubscription)
    returns the correct value immediately without a stale cache.

    Side-effects (best-effort, never fail the request):
      - CompanySubscription.status set to 'suspended'
      - All public-schema users for this tenant deactivated (is_active=False)
      - Live Django sessions for those users deleted
    """
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, pk):
        _ensure_public()
        try:
            tenant = Tenant.objects.select_related("company").get(pk=pk)
        except Tenant.DoesNotExist:
            return Response({"detail": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        if tenant.schema_name in PROTECTED_SCHEMAS:
            return Response(
                {"detail": "Cannot suspend a protected schema."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        reason = request.data.get("reason", "")

        with transaction.atomic():
            # 1. Suspend the Tenant record
            tenant.status = "suspended"
            tenant.save(update_fields=["status", "updated_at"])

            # 2. Suspend CompanySubscription (authoritative source for get_status() in serializer)
            try:
                from apps.subscriptions.models import CompanySubscription
                company = tenant.company
                if company:
                    sub = CompanySubscription.objects.filter(company=company).first()
                    if sub and sub.status not in ("cancelled", "expired"):
                        sub.status = "suspended"
                        sub.save(update_fields=["status", "updated_at"])
            except Exception as sync_err:
                logger.warning(
                    "TenantSuspend: CompanySubscription sync failed for %s: %s",
                    tenant.subdomain, sync_err,
                )

            # 3. Deactivate public-schema users belonging to this tenant
            try:
                User.objects.filter(
                    Q(tenant=tenant) | Q(company=tenant.company)
                ).update(is_active=False)
            except Exception as user_err:
                logger.warning(
                    "TenantSuspend: user deactivation failed for %s: %s",
                    tenant.subdomain, user_err,
                )

        # 4. Best-effort: delete live sessions for those users
        try:
            from django.contrib.sessions.models import Session
            deactivated_ids = set(
                User.objects.filter(
                    Q(tenant=tenant) | Q(company=tenant.company)
                ).values_list("id", flat=True)
            )
            for sess in Session.objects.all().iterator():
                try:
                    data = sess.get_decoded()
                    uid = data.get("_auth_user_id")
                    if uid and int(uid) in deactivated_ids:
                        sess.delete()
                except Exception:
                    continue
        except Exception as sess_err:
            logger.warning(
                "TenantSuspend: session cleanup failed for %s: %s",
                tenant.subdomain, sess_err,
            )

        _log_action(
            request.user, "update", "Tenant",
            object_repr=tenant.subdomain,
            object_id=tenant.id,
            changes={"action": "suspend", "reason": reason},
            request=request,
        )

        tenant.refresh_from_db()
        serialized = TenantListSerializer(tenant).data
        serialized["detail"] = (
            "Users have been deactivated and will see a contact-support message."
        )
        return Response(serialized)


class TenantActivateView(APIView):
    """
    POST /api/v1/superadmin/tenants/<pk>/activate/

    Reactivates a suspended/expired tenant. Syncs CompanySubscription.status
    back to "active", optionally extends or overrides the subscription expiry,
    and re-enables all public-schema users for this tenant.
    """
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, pk):
        _ensure_public()
        try:
            tenant = Tenant.objects.select_related("company").get(pk=pk)
        except Tenant.DoesNotExist:
            return Response({"detail": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        tenant.status = "active"
        extend_days = request.data.get("extend_days")
        set_expiry_date = request.data.get("set_expiry_date")

        today = timezone.now().date()

        if set_expiry_date:
            import datetime as _dt
            try:
                parsed_date = _dt.date.fromisoformat(str(set_expiry_date))
                tenant.subscription_expiry = parsed_date
            except (ValueError, TypeError):
                return Response(
                    {"detail": "Invalid set_expiry_date format. Use YYYY-MM-DD."},
                    status=400,
                )
        elif extend_days:
            base = (
                tenant.subscription_expiry
                if tenant.subscription_expiry and tenant.subscription_expiry > today
                else today
            )
            tenant.subscription_expiry = base + timedelta(days=int(extend_days))

        tenant.save(update_fields=["status", "subscription_expiry", "updated_at"])

        # Sync CompanySubscription
        try:
            from apps.subscriptions.models import CompanySubscription
            from django.utils import timezone as tz
            company = tenant.company
            if company:
                subscription = CompanySubscription.objects.filter(company=company).first()
                if subscription:
                    subscription.status = "active"
                    sub_fields = ["status", "updated_at"]
                    new_expiry = tenant.subscription_expiry
                    if new_expiry:
                        subscription.current_period_end = tz.datetime.combine(
                            new_expiry,
                            tz.datetime.min.time(),
                            tzinfo=tz.get_current_timezone(),
                        )
                        sub_fields.append("current_period_end")
                    if not subscription.current_period_start:
                        subscription.current_period_start = tz.now()
                        sub_fields.append("current_period_start")
                    subscription.save(update_fields=sub_fields)
        except Exception as sync_err:
            logger.warning(
                "TenantActivate: CompanySubscription sync failed for %s: %s",
                tenant.subdomain, sync_err,
            )

        # Re-activate tenant users
        try:
            User.objects.filter(
                Q(tenant=tenant) | Q(company=tenant.company)
            ).update(is_active=True)
        except Exception as user_err:
            logger.warning(
                "TenantActivate: user reactivation failed for %s: %s",
                tenant.subdomain, user_err,
            )

        _log_action(
            request.user, "update", "Tenant",
            object_repr=tenant.subdomain,
            object_id=tenant.id,
            changes={
                "action": "activate",
                "extend_days": extend_days,
                "set_expiry_date": str(set_expiry_date) if set_expiry_date else None,
            },
            request=request,
        )

        tenant.refresh_from_db()
        return Response(TenantListSerializer(tenant).data)


class TenantSupportEmailView(APIView):
    """
    Superadmin support endpoint for repairing tenant contact/login emails.

    GET returns the public company email and tenant-schema admin users.
    PATCH updates company_email and/or one tenant admin login email.
    """
    permission_classes = SUPERADMIN_PERMS
    admin_roles = ["admin", "super_admin", "superadmin"]

    def _clean_email(self, value, field_name):
        if value in (None, ""):
            return None
        email = User.objects.normalize_email(str(value).strip()).lower()
        try:
            validate_email(email)
        except DjangoValidationError:
            raise ValueError(f"{field_name} must be a valid email address.")
        return email

    def _admin_payload(self, tenant):
        admins = []
        with schema_context(tenant.schema_name):
            for user in User.objects.filter(role__in=self.admin_roles).order_by("-is_superuser", "id"):
                admins.append({
                    "id": user.id,
                    "email": user.email,
                    "name": user.get_full_name() or user.email,
                    "is_superuser": user.is_superuser,
                    "is_active": user.is_active,
                })
        return admins

    def get(self, request, pk):
        _ensure_public()
        tenant = get_object_or_404(Tenant.objects.select_related("company"), pk=pk)
        try:
            admin_users = self._admin_payload(tenant)
        except Exception as exc:
            logger.warning("Failed to read tenant admins for %s: %s", tenant.schema_name, exc)
            admin_users = []

        return Response({
            "tenant_id": str(tenant.id),
            "tenant_subdomain": tenant.subdomain,
            "company_email": tenant.company.email if tenant.company else "",
            "admin_users": admin_users,
        })

    def patch(self, request, pk):
        _ensure_public()
        tenant = get_object_or_404(Tenant.objects.select_related("company"), pk=pk)

        try:
            company_email = self._clean_email(request.data.get("company_email"), "company_email")
            tenant_admin_email = self._clean_email(
                request.data.get("tenant_admin_email") or request.data.get("login_email"),
                "tenant_admin_email",
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        user_id = request.data.get("user_id")
        if not company_email and not tenant_admin_email:
            return Response(
                {"detail": "Provide company_email or tenant_admin_email."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if company_email and Company.objects.filter(email__iexact=company_email).exclude(pk=tenant.company_id).exists():
            return Response(
                {"detail": "Another company already uses this email."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with schema_context(tenant.schema_name):
                target_user = None
                if tenant_admin_email:
                    if user_id:
                        target_user = User.objects.filter(pk=user_id, role__in=self.admin_roles).first()
                    if not target_user:
                        target_user = (
                            User.objects.filter(role__in=self.admin_roles)
                            .order_by("-is_superuser", "id")
                            .first()
                        )
                    if not target_user:
                        return Response(
                            {"detail": "No tenant admin user was found to update."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    if User.objects.filter(email__iexact=tenant_admin_email).exclude(pk=target_user.pk).exists():
                        return Response(
                            {"detail": "Another tenant user already uses this login email."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
        except Exception as exc:
            logger.exception("Failed validating tenant admin email for %s", tenant.schema_name)
            return Response(
                {"detail": f"Could not inspect tenant users: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        changes = {}
        with transaction.atomic():
            if company_email and tenant.company and (tenant.company.email or "").lower() != company_email:
                changes["company_email"] = {"from": tenant.company.email, "to": company_email}
                tenant.company.email = company_email
                tenant.company.save(update_fields=["email", "updated_at"])

            if tenant_admin_email:
                with schema_context(tenant.schema_name):
                    target_user = None
                    if user_id:
                        target_user = User.objects.filter(pk=user_id, role__in=self.admin_roles).first()
                    if not target_user:
                        target_user = (
                            User.objects.filter(role__in=self.admin_roles)
                            .order_by("-is_superuser", "id")
                            .first()
                        )
                    old_email = target_user.email
                    if (old_email or "").lower() != tenant_admin_email:
                        changes["tenant_admin_email"] = {"from": old_email, "to": tenant_admin_email}
                        target_user.email = tenant_admin_email
                        target_user.is_verified = True
                        target_user.save(update_fields=["email", "is_verified"])

        _ensure_public()
        if changes:
            _log_action(
                request.user,
                "update",
                "TenantSupportEmail",
                object_repr=tenant.subdomain,
                object_id=tenant.id,
                changes=changes,
                request=request,
            )

        tenant.refresh_from_db()
        data = TenantDetailSerializer(tenant).data
        data["support_email_info"] = {
            "company_email": tenant.company.email if tenant.company else "",
            "admin_users": self._admin_payload(tenant),
        }
        data["detail"] = "Tenant support email details updated."
        return Response(data)


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

        is_active = self.request.query_params.get("is_active")
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() == "true")

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
    """List payments across the platform.
    
    Query params:
        source: 'platform' | 'tenant' | 'all' (default: 'all')
        status: filter by payment status
        search: search company name / receipt
        page, page_size: pagination
    """
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        source = request.query_params.get("source", "all")
        status_filter = request.query_params.get("status")
        search = request.query_params.get("search", "")
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", PAGE_SIZE))

        results = []

        # ── Platform (subscription) payments ──
        if source in ("all", "platform"):
            results.extend(self._get_platform_payments(status_filter, search))

        # ── Tenant-level payments (from each tenant schema) ──
        if source in ("all", "tenant"):
            results.extend(self._get_tenant_payments(status_filter, search))

        # Sort combined by date desc
        results.sort(key=lambda x: x["created_at"], reverse=True)

        total = len(results)
        start = (page - 1) * page_size
        end = start + page_size

        return Response({
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": results[start:end],
        })

    def _get_platform_payments(self, status_filter, search):
        """Fetch SubscriptionPayment records from the public schema."""
        data = []
        try:
            from apps.subscriptions.models import SubscriptionPayment
            qs = SubscriptionPayment.objects.select_related(
                "subscription", "subscription__company", "subscription__plan"
            ).order_by("-created_at")

            if status_filter:
                qs = qs.filter(status=status_filter.lower())
            if search:
                qs = qs.filter(
                    Q(subscription__company__name__icontains=search)
                    | Q(mpesa_receipt__icontains=search)
                    | Q(payhero_reference__icontains=search)
                    | Q(bank_reference__icontains=search)
                )

            for p in qs[:200]:
                ref = p.mpesa_receipt or p.payhero_reference or p.bank_reference or ""
                company_name = "—"
                plan_name = ""
                if p.subscription:
                    if p.subscription.company:
                        company_name = p.subscription.company.name
                    if p.subscription.plan:
                        plan_name = p.subscription.plan.name

                data.append({
                    "id": str(p.id),
                    "source": "platform",
                    "company_name": company_name,
                    "plan_name": plan_name,
                    "customer_name": "",
                    "amount": str(p.amount),
                    "currency": p.currency,
                    "status": p.status,
                    "payment_method": p.payment_method or "",
                    "service_type": "subscription",
                    "reference": ref,
                    "created_at": p.created_at.isoformat(),
                })
        except Exception as e:
            logger.exception("PaymentListView platform error: %s", e)
        return data

    def _get_tenant_payments(self, status_filter, search):
        """Fetch billing.Payment records across all tenant schemas via raw SQL."""
        data = []
        try:
            tenants = (
                Tenant.objects.filter(status__in=["active", "trial"])
                .exclude(schema_name__in=PROTECTED_SCHEMAS)
                .select_related("company")
            )

            for tenant in tenants:
                schema = tenant.schema_name
                company_name = tenant.company.name if tenant.company else tenant.subdomain

                where_clauses = []
                params = []

                if status_filter:
                    where_clauses.append("p.status = %s")
                    params.append(status_filter.upper())
                if search:
                    where_clauses.append(
                        "(p.payer_name ILIKE %s OR p.mpesa_receipt ILIKE %s OR p.payment_reference ILIKE %s)"
                    )
                    s = f"%{search}%"
                    params.extend([s, s, s])

                where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

                try:
                    with connection.cursor() as cur:
                        cur.execute(f'SET search_path TO "{schema}"')
                        cur.execute(
                            'SELECT p.id, p.amount, p.status, p.mpesa_receipt, '
                            'p.payment_reference, p.payer_name, p.payment_date, '
                            'p.currency, '
                            'CASE WHEN p.hotspot_session_id IS NOT NULL '
                            "  THEN 'hotspot' "
                            '  WHEN p.customer_id IS NOT NULL '
                            "  THEN 'pppoe' "
                            "  ELSE 'other' END AS service_type "
                            'FROM billing_payment p '
                            f'{where_sql} '
                            'ORDER BY p.payment_date DESC LIMIT 100',
                            params,
                        )
                        for row in cur.fetchall():
                            data.append({
                                "id": str(row[0]),
                                "source": "tenant",
                                "company_name": company_name,
                                "plan_name": "",
                                "customer_name": row[5] or "",
                                "amount": str(row[1]),
                                "currency": row[7] or "KES",
                                "status": (row[2] or "").lower(),
                                "payment_method": "M-Pesa",
                                "service_type": row[8],
                                "reference": row[3] or row[4] or "",
                                "created_at": row[6].isoformat() if row[6] else "",
                            })
                except Exception as exc:
                    logger.warning("PaymentList tenant error for %s: %s", schema, exc)
        except Exception as e:
            logger.exception("PaymentListView tenant error: %s", e)
        return data


class PaymentSummaryView(APIView):
    """Revenue summary for dashboard cards — includes platform + tenant revenue."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        now = timezone.now()
        last_month_start = (now.replace(day=1) - timedelta(days=1)).replace(day=1)

        # ── Platform subscription revenue ──
        platform_total = Decimal("0.00")
        platform_this_month = Decimal("0.00")
        platform_last_month = Decimal("0.00")
        try:
            from apps.subscriptions.models import SubscriptionPayment
            completed = SubscriptionPayment.objects.filter(status="completed")
            platform_total = completed.aggregate(t=Sum("amount"))["t"] or Decimal("0.00")
            platform_this_month = completed.filter(
                created_at__year=now.year, created_at__month=now.month
            ).aggregate(t=Sum("amount"))["t"] or Decimal("0.00")
            last_month_end = now.replace(day=1) - timedelta(days=1)
            platform_last_month = completed.filter(
                created_at__date__gte=last_month_start, created_at__date__lte=last_month_end
            ).aggregate(t=Sum("amount"))["t"] or Decimal("0.00")
        except Exception:
            pass

        # ── Aggregate tenant-level revenue ──
        tenant_total = Decimal("0.00")
        tenant_this_month = Decimal("0.00")
        tenant_last_month = Decimal("0.00")
        try:
            tenants = Tenant.objects.filter(status__in=["active", "trial"]).exclude(
                schema_name__in=PROTECTED_SCHEMAS
            )
            for tenant in tenants:
                schema = tenant.schema_name
                try:
                    with connection.cursor() as cur:
                        cur.execute(f'SET search_path TO "{schema}"')
                        cur.execute(
                            "SELECT COALESCE(SUM(amount), 0) FROM billing_payment "
                            "WHERE status = 'COMPLETED'"
                        )
                        tenant_total += Decimal(str(cur.fetchone()[0]))

                        cur.execute(
                            "SELECT COALESCE(SUM(amount), 0) FROM billing_payment "
                            "WHERE status = 'COMPLETED' "
                            "AND EXTRACT(YEAR FROM payment_date) = %s "
                            "AND EXTRACT(MONTH FROM payment_date) = %s",
                            [now.year, now.month],
                        )
                        tenant_this_month += Decimal(str(cur.fetchone()[0]))

                        cur.execute(
                            "SELECT COALESCE(SUM(amount), 0) FROM billing_payment "
                            "WHERE status = 'COMPLETED' "
                            "AND payment_date >= %s AND payment_date < %s",
                            [last_month_start, now.replace(day=1)],
                        )
                        tenant_last_month += Decimal(str(cur.fetchone()[0]))
                except Exception as exc:
                    logger.warning("PaymentSummary tenant error for %s: %s", schema, exc)
        except Exception:
            pass

        combined_last = platform_last_month + tenant_last_month
        combined_this = platform_this_month + tenant_this_month
        pct_change = 0.0
        if combined_last > 0:
            pct_change = round(float((combined_this - combined_last) / combined_last * 100), 1)

        return Response({
            "total_revenue": float(platform_total + tenant_total),
            "this_month": float(combined_this),
            "last_month": float(combined_last),
            "pct_change": pct_change,
            "tenant_total_revenue": float(tenant_total),
            "tenant_this_month": float(tenant_this_month),
            "combined_total": float(platform_total + tenant_total),
            "combined_this_month": float(combined_this),
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

        # ── NEW: LIVE METERED BILLING STATS ──
        from apps.subscriptions.models import BillingCycle
        
        metered_stats = {
            "is_metered": False,
            "pppoe_clients": 0,
            "hotspot_revenue": 0.0,
            "estimated_total": 0.0,
            "cycle_end": None
        }

        try:
            # Get active billing cycle for this tenant
            active_cycle = BillingCycle.objects.filter(tenant=tenant, status='active').first()
            
            if active_cycle and active_cycle.subscription and active_cycle.subscription.plan:
                metered_stats.update({
                    "is_metered": active_cycle.subscription.plan.is_metered,
                    "pppoe_clients": active_cycle.calculate_total_pppoe(),
                    "hotspot_revenue": float(active_cycle.hotspot_revenue_accumulated),
                    "estimated_total": float(active_cycle.calculate_total_charge()),
                    "cycle_end": active_cycle.end_date.isoformat() if active_cycle.end_date else None
                })
        except Exception as e:
            logger.warning(f"Could not fetch metered billing stats for tenant {tenant.subdomain}: {e}")
        
        result["metered_usage"] = metered_stats

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
    """Generate a JWT so the superadmin can access a tenant's panel.

    POST /api/v1/superadmin/tenants/<pk>/impersonate/
    Body (optional): { "use_own_account": true }

    - Default: generates a JWT for the tenant's admin user.
    - use_own_account=true: generates a JWT for the *superadmin* user so they
      keep their own identity (the CompanyContextMiddleware patches
      request.user.company on the fly for superadmin users on tenant subdomains).
    """
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, pk):
        _ensure_public()
        try:
            tenant = Tenant.objects.select_related("company").get(pk=pk)
        except Tenant.DoesNotExist:
            return Response({"detail": "Tenant not found"}, status=status.HTTP_404_NOT_FOUND)

        use_own = request.data.get("use_own_account", False)

        if use_own:
            # Issue tokens for the superadmin themselves
            target_user = request.user
        else:
            # Find the tenant's admin user
            target_user = (
                User.objects
                .filter(tenant=tenant, role="admin", is_active=True)
                .order_by("date_joined")
                .first()
            )
            if not target_user:
                return Response(
                    {"detail": "No active admin user found for this tenant."},
                    status=status.HTTP_404_NOT_FOUND,
                )

        # Generate JWT for the target user
        try:
            from rest_framework_simplejwt.tokens import RefreshToken
            refresh = RefreshToken.for_user(target_user)
            tokens = {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            }
        except Exception as e:
            return Response({"detail": f"Token generation failed: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Determine the tenant panel URL
        from django.conf import settings as conf
        frontend_url = getattr(conf, 'FRONTEND_URL', 'http://localhost:3000')
        base_domain = getattr(conf, 'TENANT_BASE_DOMAIN', 'localhost')
        # Parse protocol from FRONTEND_URL
        protocol = 'https' if 'https' in frontend_url else 'http'
        panel_url = f"{protocol}://{tenant.subdomain}.{base_domain}/admin"

        _log_action(
            request.user, "login", "Tenant",
            object_repr=f"Impersonated {tenant.subdomain} as {target_user.email}",
            object_id=tenant.id,
            changes={"impersonated_user": target_user.email, "use_own_account": use_own},
            request=request,
        )

        return Response({
            "access": tokens["access"],
            "refresh": tokens["refresh"],
            "user": {
                "id": target_user.id,
                "email": target_user.email,
                "first_name": target_user.first_name,
                "last_name": target_user.last_name,
                "role": target_user.role,
                "is_superuser": target_user.is_superuser,
            },
            "tenant": {
                "subdomain": tenant.subdomain,
                "company_name": tenant.company.name,
            },
            "panel_url": panel_url,
        })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CHANGELOG MANAGEMENT (Superadmin CRUD)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class SuperadminChangelogListView(APIView):
    """List and Create Changelogs from the Superadmin Panel"""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        changelogs = Changelog.objects.all()
        return Response(ChangelogSerializer(changelogs, many=True).data)

    def post(self, request):
        _ensure_public()
        serializer = ChangelogSerializer(data=request.data)
        if serializer.is_valid():
            notification_channels = serializer.get_notification_channels_requested()
            changelog = serializer.save()
            if notification_channels and changelog.is_published:
                queue_changelog_notifications(changelog.id, notification_channels)
            _log_action(request.user, "create", "Changelog", changelog.title, changelog.id, request=request)
            response_data = ChangelogSerializer(changelog).data
            response_data["notification_request"] = {
                "channels": notification_channels,
                "queued": bool(notification_channels and changelog.is_published),
            }
            return Response(response_data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SuperadminChangelogDetailView(APIView):
    """Update and Delete Changelogs from the Superadmin Panel"""
    permission_classes = SUPERADMIN_PERMS

    def patch(self, request, pk):
        _ensure_public()
        try:
            changelog = Changelog.objects.get(pk=pk)
        except Changelog.DoesNotExist:
            return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)
            
        serializer = ChangelogSerializer(changelog, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            _log_action(request.user, "update", "Changelog", changelog.title, changelog.id, request=request)
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        _ensure_public()
        try:
            changelog = Changelog.objects.get(pk=pk)
            title = changelog.title
            changelog.delete()
            _log_action(request.user, "delete", "Changelog", title, pk, request=request)
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Changelog.DoesNotExist:
            return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FEATURE REQUEST MANAGEMENT (Superadmin)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class SuperadminFeatureListView(APIView):
    """List all feature requests for the Superadmin Roadmap"""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        with schema_context(get_public_schema_name()):
            requests = FeatureRequest.objects.all()
            serializer = FeatureRequestSerializer(requests, many=True, context={'request': request})
            return Response(serializer.data)


class SuperadminFeatureDetailView(APIView):
    """Update status or add comments to a feature request"""
    permission_classes = SUPERADMIN_PERMS

    def patch(self, request, pk):
        with schema_context(get_public_schema_name()):
            feature = get_object_or_404(FeatureRequest, pk=pk)
            serializer = FeatureRequestSerializer(feature, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                
                # Log the action
                _log_action(
                    request.user, 
                    "update", 
                    "FeatureRequest",
                    object_repr=feature.title,
                    object_id=feature.id,
                    changes=serializer.validated_data,
                    request=request,
                )
                
                return Response(serializer.data)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  BILLING CYCLES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class BillingCycleListView(APIView):
    """List all billing cycles across tenants."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        from apps.subscriptions.models import BillingCycle
        from apps.superadmin.serializers import BillingCycleSerializer

        status_filter = request.query_params.get("status")
        search = request.query_params.get("search", "")
        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", PAGE_SIZE))

        qs = BillingCycle.objects.select_related(
            "tenant", "tenant__company", "subscription", "subscription__plan",
        ).order_by("-start_date")

        if status_filter:
            qs = qs.filter(status=status_filter)
        if search:
            qs = qs.filter(
                Q(tenant__company__name__icontains=search)
                | Q(tenant__subdomain__icontains=search)
                | Q(invoice_reference__icontains=search)
            )

        total = qs.count()
        start = (page - 1) * page_size
        cycles = qs[start:start + page_size]

        return Response({
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": BillingCycleSerializer(cycles, many=True).data,
        })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TENANT USER LEDGER (IMMUTABLE AUDIT TRAIL)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TenantUserLedgerListView(APIView):
    """
    List immutable tenant user ledger entries across all tenants.
    Supports filtering by tenant, event type, user type, and date range.

    GET /api/v1/superadmin/user-ledger/
    Query params:
      - tenant_id: UUID filter
      - event: customer_created, service_created, etc.
      - user_type: pppoe, hotspot, static, etc.
      - search: free-text search on customer_name, username, customer_code
      - date_from, date_to: ISO date range filters
      - page, page_size: pagination
    """
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        from apps.subscriptions.models import BillingCycle, CompanySubscription, TenantUserLedger
        from apps.superadmin.serializers import TenantUserLedgerSerializer

        tenant_id = request.query_params.get("tenant_id")
        event_filter = request.query_params.get("event")
        user_type_filter = request.query_params.get("user_type")
        search = request.query_params.get("search", "")
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        try:
            page = max(int(request.query_params.get("page", 1)), 1)
            page_size = max(min(int(request.query_params.get("page_size", PAGE_SIZE)), 200), 1)
        except (TypeError, ValueError):
            page = 1
            page_size = PAGE_SIZE

        qs = TenantUserLedger.objects.select_related(
            "tenant", "tenant__company",
        ).order_by("-created_at")

        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        if event_filter:
            qs = qs.filter(event=event_filter)
        if user_type_filter:
            qs = qs.filter(user_type=user_type_filter)
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        if search:
            qs = qs.filter(
                Q(customer_name__icontains=search)
                | Q(username__icontains=search)
                | Q(customer_code__icontains=search)
                | Q(phone_number__icontains=search)
                | Q(tenant__company__name__icontains=search)
            )

        total = qs.count()
        start = (page - 1) * page_size
        entries = qs[start:start + page_size]

        hotspot_revenue_total = Decimal("0.00")
        hotspot_share_total = Decimal("0.00")
        hotspot_tenants = []
        active_cycle_count = 0

        try:
            metered_subscriptions = CompanySubscription.objects.filter(
                status__in=['active', 'trialing'],
                plan__is_metered=True,
                company__tenant__isnull=False,
            ).select_related("company__tenant", "plan")
            if tenant_id:
                metered_subscriptions = metered_subscriptions.filter(company__tenant__id=tenant_id)

            for subscription in metered_subscriptions:
                tenant = getattr(subscription.company, "tenant", None)
                if not tenant:
                    continue
                BillingCycle.objects.get_or_create(
                    tenant=tenant,
                    subscription=subscription,
                    status='active',
                    defaults={
                        "start_date": subscription.current_period_start or timezone.now(),
                        "end_date": subscription.current_period_end or (timezone.now() + timedelta(days=30)),
                    },
                )

            active_cycles = BillingCycle.objects.filter(status='active').select_related(
                "tenant", "tenant__company", "subscription__plan"
            )
            if tenant_id:
                active_cycles = active_cycles.filter(tenant_id=tenant_id)
            active_cycle_count = active_cycles.count()
        except Exception as exc:
            logger.warning("User ledger billing-cycle summary setup failed: %s", exc)
            active_cycles = BillingCycle.objects.none()

        for cycle in active_cycles:
            try:
                fallback_pct = (
                    Decimal(str(cycle.subscription.plan.hotspot_revenue_share_pct or 0))
                    if cycle.subscription and cycle.subscription.plan
                    else Decimal("0.00")
                ) or Decimal("3.00")
                if not cycle.snapshot_hotspot_share_pct:
                    cycle.snapshot_hotspot_share_pct = fallback_pct
                    BillingCycle.objects.filter(pk=cycle.pk).update(
                        snapshot_hotspot_share_pct=fallback_pct
                    )

                revenue = cycle.refresh_actual_hotspot_revenue()
                share = cycle.calculate_hotspot_revenue_share(revenue)
            except Exception as exc:
                logger.warning(
                    "User ledger hotspot summary failed for %s: %s",
                    getattr(cycle.tenant, "schema_name", cycle.tenant_id),
                    exc,
                )
                revenue = cycle.hotspot_revenue_accumulated or Decimal("0.00")
                share = Decimal("0.00")

            hotspot_revenue_total += revenue
            hotspot_share_total += share
            if revenue or share:
                hotspot_tenants.append({
                    "tenant_id": str(cycle.tenant_id),
                    "tenant_name": getattr(getattr(cycle.tenant, "company", None), "name", cycle.tenant.schema_name),
                    "tenant_schema": cycle.tenant.schema_name,
                    "billing_cycle_id": str(cycle.id),
                    "hotspot_revenue": str(revenue),
                    "hotspot_share_pct": str(cycle.snapshot_hotspot_share_pct),
                    "hotspot_share_amount": str(share),
                })

        hotspot_tenants.sort(
            key=lambda row: Decimal(str(row["hotspot_revenue"] or 0)),
            reverse=True,
        )

        return Response({
            "count": total,
            "page": page,
            "page_size": page_size,
            "summary": {
                "active_cycle_count": active_cycle_count,
                "hotspot_revenue_total": str(hotspot_revenue_total),
                "hotspot_share_total": str(hotspot_share_total),
                "hotspot_tenants": hotspot_tenants,
            },
            "results": TenantUserLedgerSerializer(entries, many=True).data,
        })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  TUMA SUBSCRIPTION PAYMENTS (ISPs paying Netily)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _decimal_money(value):
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def _subscription_invoice_admins(tenant):
    with schema_context(tenant.schema_name):
        admin_roles = ["admin", "super_admin", "superadmin", "accountant", "support"]
        admins = User.objects.filter(
            is_active=True,
            role__in=admin_roles,
        ).values("id", "email", "phone_number", "first_name", "last_name")
        return list(admins)


def _get_or_create_subscription_invoice(cycle, *, create=False):
    invoice_id = cycle.invoice_reference
    invoice = None

    actual_hotspot_revenue = _decimal_money(cycle.refresh_actual_hotspot_revenue())
    pppoe_count = cycle.calculate_total_pppoe()
    pppoe_charge = _decimal_money(cycle.calculate_pppoe_charge())
    hotspot_share = _decimal_money(cycle.calculate_hotspot_revenue_share(actual_hotspot_revenue))
    minimum_adjustment = _decimal_money(cycle.calculate_minimum_adjustment(actual_hotspot_revenue))
    total_due = _decimal_money(cycle.calculate_billable_usage_charge(actual_hotspot_revenue))

    with schema_context(cycle.tenant.schema_name):
        from apps.billing.models import Invoice, InvoiceItem
        from apps.customers.models import Customer

        if invoice_id:
            try:
                invoice = Invoice.objects.filter(pk=invoice_id).first()
            except (TypeError, ValueError):
                invoice = None
        if invoice or not create:
            return invoice

        billing_user, _ = User.objects.get_or_create(
            email="billing@netily.io",
            defaults={
                "first_name": "Netily",
                "last_name": "Platform",
                "role": "admin",
                "is_staff": True,
                "is_active": True,
            },
        )
        sys_customer, _ = Customer.objects.get_or_create(
            customer_code="NET-001",
            defaults={"user": billing_user, "status": "active"},
        )

        now_ts = timezone.now()
        due_date = (
            cycle.grace_ends_at.date()
            if cycle.grace_ends_at
            else max(cycle.end_date, now_ts + timedelta(days=1)).date()
        )

        invoice = Invoice.objects.create(
            invoice_number=f'NET-BILL-{now_ts.strftime("%y%m%d%H%M%S")}',
            customer=sys_customer,
            subtotal=total_due,
            total_amount=total_due,
            status="ISSUED",
            service_period_start=cycle.start_date.date(),
            service_period_end=cycle.end_date.date(),
            due_date=due_date,
            billing_date=now_ts.date(),
            notes="Netily platform subscription invoice.",
        )
        if pppoe_charge:
            InvoiceItem.objects.create(
                invoice=invoice,
                description=f"PPPoE Client Footprint ({pppoe_count} users @ KES {cycle.snapshot_pppoe_price} each)",
                quantity=pppoe_count,
                unit_price=cycle.snapshot_pppoe_price,
                tax_rate=0,
                tax_amount=0,
                total=pppoe_charge,
            )
        if hotspot_share:
            InvoiceItem.objects.create(
                invoice=invoice,
                description=f"Hotspot Revenue Share ({cycle.snapshot_hotspot_share_pct}% of KES {actual_hotspot_revenue:,.2f})",
                quantity=1,
                unit_price=hotspot_share,
                tax_rate=0,
                tax_amount=0,
                total=hotspot_share,
            )
        if minimum_adjustment:
            InvoiceItem.objects.create(
                invoice=invoice,
                description=f"Monthly Minimum Charge Adjustment (minimum KES {cycle.snapshot_base_fee or Decimal('500.00'):,.2f})",
                quantity=1,
                unit_price=minimum_adjustment,
                tax_rate=0,
                tax_amount=0,
                total=minimum_adjustment,
            )

    update_fields = {"invoice_reference": str(invoice.id)}
    if cycle.status == "active":
        update_fields["status"] = "invoiced"
    if not cycle.grace_ends_at:
        update_fields["grace_ends_at"] = max(cycle.end_date, timezone.now() + timedelta(days=1))
    cycle.__class__.objects.filter(pk=cycle.pk).update(**update_fields)
    for field, value in update_fields.items():
        setattr(cycle, field, value)
    return invoice


def _subscription_invoice_payload(cycle, *, include_recipients=False):
    tenant = cycle.tenant
    company = getattr(tenant, "company", None)
    invoice_snapshot = None
    receipt_snapshot = None

    try:
        actual_hotspot_revenue = _decimal_money(cycle.refresh_actual_hotspot_revenue())
    except Exception as exc:
        logger.warning("Failed to refresh hotspot revenue for cycle %s: %s", cycle.id, exc)
        actual_hotspot_revenue = _decimal_money(cycle.hotspot_revenue_accumulated)

    pppoe_count = cycle.calculate_total_pppoe()
    pppoe_charge = _decimal_money(cycle.calculate_pppoe_charge())
    hotspot_share = _decimal_money(cycle.calculate_hotspot_revenue_share(actual_hotspot_revenue))
    usage_subtotal = _decimal_money(cycle.calculate_usage_subtotal(actual_hotspot_revenue))
    minimum_adjustment = _decimal_money(cycle.calculate_minimum_adjustment(actual_hotspot_revenue))
    calculated_total = _decimal_money(cycle.calculate_billable_usage_charge(actual_hotspot_revenue))

    try:
        invoice = _get_or_create_subscription_invoice(cycle, create=False)
    except Exception as exc:
        logger.warning("Failed to load linked tenant invoice for cycle %s: %s", cycle.id, exc)
        invoice = None
    try:
        from apps.subscriptions.billing_lifecycle import latest_subscription_receipt

        receipt_snapshot = latest_subscription_receipt(cycle.subscription)
    except Exception as exc:
        logger.warning("Failed loading subscription receipt for cycle %s: %s", cycle.id, exc)
    effective_total = calculated_total
    if invoice:
        with schema_context(tenant.schema_name):
            from apps.billing.models import Invoice

            tenant_invoice = Invoice.objects.filter(pk=invoice.pk).first()
            if tenant_invoice:
                manual_adjustment = tenant_invoice.items.filter(service_type="netily_manual_adjustment").aggregate(
                    total=Sum("total")
                )["total"] or Decimal("0.00")
                manual_adjustment_item = (
                    tenant_invoice.items.filter(service_type="netily_manual_adjustment")
                    .order_by("-id")
                    .first()
                )
                invoice_snapshot = {
                    "id": tenant_invoice.id,
                    "invoice_number": tenant_invoice.invoice_number,
                    "status": tenant_invoice.status,
                    "subtotal": str(_decimal_money(tenant_invoice.subtotal or calculated_total)),
                    "discount_amount": str(_decimal_money(tenant_invoice.discount_amount)),
                    "manual_adjustment_amount": str(_decimal_money(manual_adjustment)),
                    "manual_adjustment_description": manual_adjustment_item.description if manual_adjustment_item else "",
                    "total_amount": str(_decimal_money(tenant_invoice.total_amount)),
                    "amount_paid": str(_decimal_money(tenant_invoice.amount_paid)),
                    "balance": str(_decimal_money(tenant_invoice.balance)),
                    "due_date": tenant_invoice.due_date.isoformat() if tenant_invoice.due_date else None,
                    "paid_at": tenant_invoice.paid_at.isoformat() if tenant_invoice.paid_at else None,
                    "notes": tenant_invoice.notes or "",
                    "internal_notes": tenant_invoice.internal_notes or "",
                    "receipt": receipt_snapshot,
                }
                effective_total = _decimal_money(tenant_invoice.total_amount)

    recipients = _subscription_invoice_admins(tenant) if include_recipients else []

    return {
        "id": str(cycle.id),
        "tenant_id": str(tenant.id),
        "tenant_name": getattr(company, "name", tenant.subdomain),
        "tenant_subdomain": tenant.subdomain,
        "tenant_schema": tenant.schema_name,
        "company_email": getattr(company, "email", ""),
        "company_phone": getattr(company, "phone_number", ""),
        "status": cycle.status,
        "start_date": cycle.start_date.isoformat() if cycle.start_date else None,
        "end_date": cycle.end_date.isoformat() if cycle.end_date else None,
        "grace_ends_at": cycle.grace_ends_at.isoformat() if cycle.grace_ends_at else None,
        "invoice_reference": cycle.invoice_reference,
        "pppoe_count": pppoe_count,
        "pppoe_unit_price": str(_decimal_money(cycle.snapshot_pppoe_price)),
        "pppoe_charge": str(pppoe_charge),
        "hotspot_revenue": str(actual_hotspot_revenue),
        "hotspot_share_pct": str(_decimal_money(cycle.snapshot_hotspot_share_pct)),
        "hotspot_share": str(hotspot_share),
        "usage_subtotal": str(usage_subtotal),
        "monthly_minimum": str(_decimal_money(cycle.snapshot_base_fee or Decimal("500.00"))),
        "minimum_adjustment": str(minimum_adjustment),
        "calculated_total": str(calculated_total),
        "effective_total": str(effective_total),
        "invoice": invoice_snapshot,
        "receipt": receipt_snapshot,
        "recipients": recipients,
    }


class SubscriptionInvoiceListView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        from apps.subscriptions.models import BillingCycle

        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", PAGE_SIZE))
        status_filter = request.query_params.get("status")
        tenant_id = request.query_params.get("tenant_id")
        search = request.query_params.get("search", "").strip()

        qs = BillingCycle.objects.select_related(
            "tenant",
            "tenant__company",
            "subscription",
            "subscription__plan",
        ).order_by("-start_date")

        if status_filter and status_filter != "all":
            qs = qs.filter(status=status_filter)
        if tenant_id:
            qs = qs.filter(tenant_id=tenant_id)
        if search:
            qs = qs.filter(
                Q(tenant__subdomain__icontains=search)
                | Q(tenant__schema_name__icontains=search)
                | Q(tenant__company__name__icontains=search)
                | Q(tenant__company__email__icontains=search)
                | Q(invoice_reference__icontains=search)
            )

        total = qs.count()
        start = (page - 1) * page_size
        cycles = list(qs[start:start + page_size])
        results = [_subscription_invoice_payload(cycle) for cycle in cycles]

        return Response({
            "count": total,
            "page": page,
            "page_size": page_size,
            "summary": {
                "count": total,
                "active": qs.filter(status="active").count(),
                "invoiced": qs.filter(status="invoiced").count(),
                "paid": qs.filter(status="paid").count(),
                "calculated_total": str(_decimal_money(sum(Decimal(row.get("effective_total") or row["calculated_total"]) for row in results))),
                "hotspot_revenue": str(_decimal_money(sum(Decimal(row["hotspot_revenue"]) for row in results))),
            },
            "results": results,
        })


class SubscriptionInvoiceDetailView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def get_cycle(self, pk):
        _ensure_public()
        from apps.subscriptions.models import BillingCycle
        return get_object_or_404(
            BillingCycle.objects.select_related("tenant", "tenant__company", "subscription", "subscription__plan"),
            pk=pk,
        )

    def get(self, request, pk):
        cycle = self.get_cycle(pk)
        return Response(_subscription_invoice_payload(cycle, include_recipients=True))

    def patch(self, request, pk):
        cycle = self.get_cycle(pk)
        discount_amount = _decimal_money(request.data.get("discount_amount", "0"))
        discount_reason = (request.data.get("discount_reason") or "").strip()
        adjustment_amount = _decimal_money(request.data.get("manual_adjustment_amount", "0"))
        adjustment_description = (
            request.data.get("manual_adjustment_description")
            or request.data.get("adjustment_description")
            or "Manual custom charge"
        ).strip()
        if discount_amount < 0:
            return Response({"detail": "discount_amount cannot be negative."}, status=status.HTTP_400_BAD_REQUEST)
        if adjustment_amount < 0:
            return Response({"detail": "manual_adjustment_amount cannot be negative."}, status=status.HTTP_400_BAD_REQUEST)

        invoice = _get_or_create_subscription_invoice(cycle, create=True)
        with schema_context(cycle.tenant.schema_name):
            from apps.billing.models import Invoice, InvoiceItem
            invoice = Invoice.objects.get(pk=invoice.pk)
            manual_items = invoice.items.filter(service_type="netily_manual_adjustment")
            existing_adjustment = manual_items.aggregate(total=Sum("total"))["total"] or Decimal("0.00")
            base_subtotal = _decimal_money((invoice.subtotal or invoice.total_amount) - existing_adjustment)
            intended_subtotal = _decimal_money(base_subtotal + adjustment_amount)
            if discount_amount > intended_subtotal:
                return Response({"detail": "discount_amount cannot exceed invoice subtotal."}, status=status.HTTP_400_BAD_REQUEST)

            if adjustment_amount > 0:
                item = manual_items.order_by("id").first()
                if item:
                    item.description = adjustment_description
                    item.quantity = Decimal("1.00")
                    item.unit_price = adjustment_amount
                    item.tax_rate = Decimal("0.00")
                    item.tax_amount = Decimal("0.00")
                    item.save()
                    manual_items.exclude(pk=item.pk).delete()
                else:
                    InvoiceItem.objects.create(
                        invoice=invoice,
                        description=adjustment_description,
                        quantity=1,
                        unit_price=adjustment_amount,
                        tax_rate=0,
                        tax_amount=0,
                        service_type="netily_manual_adjustment",
                    )
            else:
                manual_items.delete()

            invoice.calculate_totals()
            invoice.refresh_from_db()
            subtotal = _decimal_money(invoice.subtotal or invoice.total_amount)
            invoice.discount_amount = discount_amount
            invoice.calculate_totals()

            notes = []
            if adjustment_amount > 0:
                notes.append(f"Manual charge applied by superadmin: KES {adjustment_amount} - {adjustment_description}")
            if discount_amount > 0:
                note = f"Manual discount applied by superadmin: KES {discount_amount}"
                if discount_reason:
                    note = f"{note} - {discount_reason}"
                notes.append(note)
            if notes:
                invoice.internal_notes = f"{invoice.internal_notes or ''}\n" + "\n".join(notes)
                invoice.internal_notes = invoice.internal_notes.strip()
                invoice.save(update_fields=["internal_notes", "updated_at"])

        _log_action(
            request.user,
            "update",
            "SubscriptionInvoice",
            object_repr=f"{cycle.tenant.subdomain} invoice adjustment",
            object_id=cycle.id,
            changes={
                "discount_amount": str(discount_amount),
                "discount_reason": discount_reason,
                "manual_adjustment_amount": str(adjustment_amount),
                "manual_adjustment_description": adjustment_description,
            },
            request=request,
        )
        cycle.refresh_from_db()
        return Response(_subscription_invoice_payload(cycle, include_recipients=True))


class SubscriptionInvoiceSendView(APIView):
    permission_classes = SUPERADMIN_PERMS

    def post(self, request, pk):
        _ensure_public()
        from apps.subscriptions.models import BillingCycle
        from apps.core.email_delivery import send_transactional_email

        cycle = get_object_or_404(
            BillingCycle.objects.select_related("tenant", "tenant__company", "subscription", "subscription__plan"),
            pk=pk,
        )
        invoice = _get_or_create_subscription_invoice(cycle, create=True)
        channel = (request.data.get("channel") or "email").lower()
        if channel not in {"email", "sms", "in_app", "all"}:
            return Response({"detail": "channel must be email, sms, in_app or all."}, status=status.HTTP_400_BAD_REQUEST)

        payload = _subscription_invoice_payload(cycle, include_recipients=True)
        invoice_payload = payload.get("invoice") or {}
        receipt_payload = payload.get("receipt") or invoice_payload.get("receipt") or {}
        invoice_status = str(invoice_payload.get("status") or "").upper()
        invoice_is_paid = cycle.status == "paid" or invoice_status == "PAID" or _decimal_money(invoice_payload.get("balance")) <= 0

        if invoice_is_paid:
            subject = f"Netily payment receipt for {payload['tenant_name']} - {invoice_payload.get('invoice_number')}"
            message = (
                "Your Netily subscription payment has been received.\n\n"
                f"Invoice: {invoice_payload.get('invoice_number')}\n"
                f"Paid amount: KES {invoice_payload.get('amount_paid') or invoice_payload.get('total_amount')}\n"
                f"Receipt: {receipt_payload.get('receipt_number') or 'Payment confirmed'}\n"
                f"Paid at: {receipt_payload.get('completed_at') or invoice_payload.get('paid_at') or 'Confirmed'}\n"
                f"Period: {cycle.start_date.date()} to {cycle.end_date.date()}\n\n"
                "Your account is active. This is your payment confirmation and receipt record."
            )
        else:
            subject = f"Netily invoice for {payload['tenant_name']} - KES {invoice_payload['total_amount']}"
            message = (
                "Your Netily subscription invoice is ready.\n\n"
                f"Invoice: {invoice_payload['invoice_number']}\n"
                f"Amount due: KES {invoice_payload['total_amount']}\n"
                f"Period: {cycle.start_date.date()} to {cycle.end_date.date()}\n"
                f"Due date: {invoice_payload['due_date'] or 'Pending'}\n\n"
                "Please open your Netily admin billing page to review and settle it."
            )
        recipients = payload["recipients"]
        email_count = 0
        notification_count = 0
        sms_count = 0

        with schema_context(cycle.tenant.schema_name):
            from apps.notifications.models import Notification
            from apps.notifications.services.notification_manager import NotificationManager

            notification_manager = NotificationManager()

            for recipient in recipients:
                email = recipient.get("email")
                phone = recipient.get("phone_number")
                user_id = recipient.get("id")

                if channel in {"email", "all"} and email:
                    result = send_transactional_email(
                        subject=subject,
                        recipient=email,
                        plain_message=message,
                        html_message=message.replace("\n", "<br>"),
                    )
                    if result.get("sent"):
                        email_count += 1

                if channel in {"in_app", "all"}:
                    notification = Notification.objects.create(
                        user_id=user_id,
                        notification_type="in_app",
                        subject=subject,
                        message=message,
                        priority=4,
                        metadata={
                            "source": "superadmin_subscription_invoice",
                            "billing_cycle_id": str(cycle.id),
                            "invoice_id": invoice.id,
                        },
                    )
                    notification_manager.send_notification(notification)
                    notification_count += 1

                if channel in {"sms", "all"} and phone:
                    notification = Notification.objects.create(
                        user_id=user_id,
                        notification_type="sms",
                        subject=subject,
                        message=(
                            (
                                f"Netily receipt {invoice_payload.get('invoice_number')}: "
                                f"KES {invoice_payload.get('amount_paid') or invoice_payload.get('total_amount')} paid. "
                                f"Receipt {receipt_payload.get('receipt_number') or 'confirmed'}."
                            )
                            if invoice_is_paid
                            else (
                                f"Netily invoice {invoice_payload['invoice_number']}: "
                                f"KES {invoice_payload['total_amount']} due {invoice_payload['due_date'] or 'soon'}. "
                                "Open admin billing to pay."
                            )
                        ),
                        recipient_phone=phone,
                        priority=4,
                        metadata={
                            "source": "superadmin_subscription_invoice",
                            "billing_cycle_id": str(cycle.id),
                            "invoice_id": invoice.id,
                        },
                    )
                    notification_manager.send_notification(notification)
                    sms_count += 1

            if not invoice_is_paid and invoice.status in {"DRAFT", "ISSUED"}:
                invoice.status = "SENT"
                invoice.save(update_fields=["status", "updated_at"])

        _log_action(
            request.user,
            "update",
            "SubscriptionInvoice",
            object_repr=f"Sent invoice {payload['invoice']['invoice_number']} to {cycle.tenant.subdomain}",
            object_id=cycle.id,
            changes={"channel": channel, "email_count": email_count, "notification_count": notification_count, "sms_count": sms_count},
            request=request,
        )

        return Response({
            "detail": "Receipt send action completed." if invoice_is_paid else "Invoice send action completed.",
            "channel": channel,
            "email_count": email_count,
            "notification_count": notification_count,
            "sms_count": sms_count,
            "invoice": _subscription_invoice_payload(cycle, include_recipients=True),
        })


class SubscriptionStkPushView(APIView):
    """Initiate an STK push to the ISP's phone via Tuma MASTER account.

    POST /api/v1/superadmin/subscriptions/pay/
    Body: { "subscription_id": "<uuid>", "phone": "2547XXXXXXXX", "amount": 5000 }
    """
    permission_classes = SUPERADMIN_PERMS

    def post(self, request):
        _ensure_public()
        from apps.subscriptions.models import CompanySubscription, SubscriptionPayment
        from apps.billing.services.tuma_service import TumaClient, TumaError

        sub_id = request.data.get("subscription_id")
        phone = request.data.get("phone", "").strip()
        amount = request.data.get("amount")

        if not sub_id or not phone or not amount:
            return Response(
                {"detail": "subscription_id, phone and amount are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            subscription = CompanySubscription.objects.select_related("plan", "company").get(pk=sub_id)
        except CompanySubscription.DoesNotExist:
            return Response({"detail": "Subscription not found."}, status=status.HTTP_404_NOT_FOUND)

        # Create a pending payment
        payment = SubscriptionPayment.objects.create(
            subscription=subscription,
            amount=amount,
            currency="KES",
            payment_method="mpesa_stk",
            phone_number=phone,
            status="processing",
        )

        try:
            client = TumaClient()
            token = client.get_master_token()
            # Use the dedicated subscription callback URL (not derived from TUMA_CALLBACK_URL)
            callback_url = getattr(settings, "TUMA_SUBSCRIPTION_CALLBACK", "")
            if not callback_url:
                # Fallback: derive from TUMA_CALLBACK_URL
                callback_url = getattr(settings, "TUMA_CALLBACK_URL", "").replace(
                    "/callback/", "/subscription-callback/"
                )
            description = f"Netily-{subscription.company.name[:20]}"

            logger.info(
                "Superadmin STK Push: phone=%s, amount=%s, callback=%s",
                phone, amount, callback_url,
            )

            result = client.stk_push(token, amount, phone, callback_url, description)
            data = result.get("data", result)

            payment.payhero_checkout_id = data.get("checkout_request_id", "")
            payment.payhero_reference = data.get("merchant_request_id", "")
            payment.save(update_fields=["payhero_checkout_id", "payhero_reference"])

            return Response({
                "payment_id": str(payment.id),
                "merchant_request_id": data.get("merchant_request_id"),
                "checkout_request_id": data.get("checkout_request_id"),
                "status": "processing",
                "message": "STK push sent. Awaiting confirmation.",
            })
        except TumaError as e:
            payment.mark_failed(str(e))
            return Response({"detail": str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        except Exception as e:
            logger.exception("SubscriptionStkPush error")
            payment.mark_failed(str(e))
            return Response({"detail": "Payment initiation failed."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SubscriptionStkCallbackView(APIView):
    """PUBLIC webhook — Tuma calls this when the ISP completes (or cancels) the STK push.

    POST /api/v1/webhooks/tuma/subscription-callback/
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        data = request.data
        merchant_id = data.get("merchant_request_id", "")
        checkout_id = data.get("checkout_request_id", "")
        result_code = data.get("result_code")

        if not merchant_id and not checkout_id:
            return Response({"success": False, "message": "Missing identifiers"}, status=400)

        from apps.subscriptions.models import SubscriptionPayment
        from django_tenants.utils import schema_context, get_public_schema_name

        with schema_context(get_public_schema_name()):
            payment = None
            if checkout_id:
                payment = SubscriptionPayment.objects.filter(payhero_checkout_id=checkout_id).first()
            if not payment and merchant_id:
                payment = SubscriptionPayment.objects.filter(payhero_reference=merchant_id).first()

        if not payment:
            logger.warning("Subscription callback: no payment for merchant=%s checkout=%s", merchant_id, checkout_id)
            return Response({"success": False, "message": "Payment not found"}, status=404)

        with schema_context(get_public_schema_name()):
            if payment.status == "completed":
                return Response({"success": True, "message": "Already processed"})

            is_success = str(result_code) == "0"

            if is_success:
                receipt = data.get("mpesa_receipt_number", "")

                # ── Atomic block: mark payment + activate subscription together ──
                # If either step fails, both are rolled back so we never have a
                # "paid but still expired" tenant.
                with transaction.atomic():
                    payment.mark_completed(mpesa_receipt=receipt)

                    sub = payment.subscription
                    # Convert trial → paid if the subscription was in any trial/pending state.
                    # Handles: trialing, expired (missed the window), pending (first-ever payment)
                    if sub.is_trial or sub.status in ('trialing', 'expired', 'pending'):
                        sub.convert_from_trial(billing_period=sub.billing_period or 'monthly')
                    else:
                        sub.extend_subscription()

                try:
                    from apps.subscriptions.billing_lifecycle import sync_subscription_invoice_payment

                    invoice = sync_subscription_invoice_payment(payment, notify=True)
                    if invoice:
                        logger.info(
                            "Subscription invoice %s marked paid for %s (receipt: %s)",
                            invoice.invoice_number,
                            sub.company.name,
                            receipt,
                        )
                except Exception as sync_err:
                    logger.warning("Failed to sync subscription invoice for %s: %s", sub.company_id, sync_err)

                # ── Mark NET-BILL invoices as paid in tenant schema ──
                # (outside atomic to avoid cross-schema transaction issues)
                try:
                    company = sub.company
                    tenant = getattr(company, 'tenant', None)
                    if not tenant and hasattr(company, 'tenant_set'):
                        tenant = company.tenant_set.first()
                    if tenant:
                        with schema_context(tenant.schema_name):
                            from apps.billing.models import Invoice, InvoiceItem
                            from django.contrib.auth import get_user_model

                            _User = get_user_model()
                            billing_user, _ = _User.objects.get_or_create(
                                email='billing@netily.io',
                                defaults={'first_name': 'Netily', 'last_name': 'Platform'}
                            )

                            from apps.customers.models import Customer as _Customer
                            sys_customer, _ = _Customer.objects.get_or_create(
                                customer_code='NET-001',
                                defaults={'user': billing_user, 'status': 'active'}
                            )

                            unpaid = Invoice.objects.filter(
                                invoice_number__startswith='NET-BILL',
                                status__in=['ISSUED', 'issued', 'pending', 'overdue'],
                            ).order_by('created_at')
                            updated = unpaid.update(
                                status='PAID',
                                amount_paid=F('total_amount'),
                                balance=Decimal('0.00'),
                                paid_at=timezone.now(),
                            )
                            if updated:
                                logger.info(
                                    "Marked %d NET-BILL invoice(s) as paid for %s (receipt: %s)",
                                    updated, company.name, receipt
                                )

                            # If no prior NET-BILL invoice existed (e.g. trial→paid first payment),
                            # create a subscription activation invoice so it appears in the invoices tab.
                            if updated == 0 and not Invoice.objects.filter(invoice_number__startswith='NET-BILL').exists():
                                plan_name = sub.plan.name if sub.plan else "Netily Platform"
                                now_ts = timezone.now()
                                new_inv = Invoice.objects.create(
                                    invoice_number=f'NET-BILL-{now_ts.strftime("%y%m%d%H%M%S")}',
                                    customer=sys_customer,
                                    subtotal=payment.amount,
                                    total_amount=payment.amount,
                                    amount_paid=payment.amount,
                                    balance=Decimal('0.00'),
                                    status='PAID',
                                    paid_at=now_ts,
                                    due_date=now_ts.date(),
                                    billing_date=now_ts.date(),
                                    service_period_start=(sub.current_period_start or now_ts).date(),
                                    service_period_end=(sub.current_period_end or now_ts).date(),
                                )
                                InvoiceItem.objects.create(
                                    invoice=new_inv,
                                    description=f'Netily Platform Subscription – {plan_name}',
                                    quantity=1,
                                    unit_price=payment.amount,
                                    tax_rate=0,
                                    tax_amount=0,
                                    total=payment.amount,
                                )
                                logger.info(
                                    "Created subscription invoice %s for %s (receipt: %s, amount: %s)",
                                    new_inv.invoice_number, company.name, receipt, payment.amount
                                )
                except Exception as inv_err:
                    logger.warning("Failed to mark/create invoices for %s: %s", sub.company_id, inv_err)

                # Fire confirmation email in background
                try:
                    from apps.subscriptions.tasks import send_cycle_activated_email
                    send_cycle_activated_email.delay(str(sub.company_id))
                except Exception as email_err:
                    logger.warning("Failed to queue cycle_activated email: %s", email_err)

                logger.info("Subscription payment %s completed. Receipt: %s", payment.id, receipt)
            else:
                reason = data.get("failure_reason") or data.get("result_desc") or "STK push failed"
                payment.mark_failed(reason)
                logger.warning("Subscription payment %s failed: %s", payment.id, reason)

        return Response({"success": True})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SUBSCRIPTION PAYMENTS (PLATFORM BILLING)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class SubscriptionPaymentListView(APIView):
    """List all ISP-to-Netily subscription payments from the public schema."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        from apps.subscriptions.models import SubscriptionPayment

        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", PAGE_SIZE))
        search = request.query_params.get("search", "").strip()
        status_filter = request.query_params.get("status", "").strip()
        tenant_id = request.query_params.get("tenant", "").strip()

        with schema_context(get_public_schema_name()):
            qs = SubscriptionPayment.objects.select_related(
                "subscription__company", "intended_plan"
            ).order_by("-created_at")

            if search:
                qs = qs.filter(
                    Q(subscription__company__name__icontains=search)
                    | Q(mpesa_receipt__icontains=search)
                    | Q(phone_number__icontains=search)
                )
            if status_filter:
                qs = qs.filter(status=status_filter)
            if tenant_id:
                qs = qs.filter(subscription__company__tenant__id=tenant_id)

            total = qs.count()
            start = (page - 1) * page_size
            payments = qs[start: start + page_size]

            results = []
            for p in payments:
                results.append({
                    "id": str(p.id),
                    "company_name": p.subscription.company.name if p.subscription and p.subscription.company else "—",
                    "plan_name": p.intended_plan.name if p.intended_plan else (
                        p.subscription.plan.name if p.subscription and p.subscription.plan else "—"
                    ),
                    "amount": str(p.amount),
                    "currency": p.currency,
                    "payment_method": p.payment_method,
                    "status": p.status,
                    "mpesa_receipt": p.mpesa_receipt or "",
                    "phone_number": p.phone_number or "",
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                    "completed_at": p.completed_at.isoformat() if p.completed_at else None,
                })

        return Response({
            "count": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
            "results": results,
        })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  LEADS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class LeadListView(APIView):
    """List leads from the public schema with pagination."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        from apps.core.models import Lead

        page = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", PAGE_SIZE))
        search = request.query_params.get("search", "").strip()

        qs = Lead.objects.all().order_by("-created_at")
        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(email__icontains=search) |
                Q(company_name__icontains=search) |
                Q(lead_source__icontains=search) |
                Q(referral_name__icontains=search) |
                Q(phone__icontains=search)
            )

        # Filter by contacted status
        contacted = request.query_params.get("contacted")
        if contacted == "true":
            qs = qs.filter(is_contacted=True)
        elif contacted == "false":
            qs = qs.filter(is_contacted=False)

        total = qs.count()
        start = (page - 1) * page_size
        leads = qs[start:start + page_size]

        results = [
            {
                "id": l.id,
                "name": l.name,
                "email": l.email,
                "phone": l.phone,
                "company_name": l.company_name,
                "lead_source": l.lead_source,
                "referral_name": l.referral_name,
                "message": l.message,
                "is_contacted": l.is_contacted,
                "contacted_at": l.contacted_at.isoformat() if l.contacted_at else None,
                "created_at": l.created_at.isoformat(),
            }
            for l in leads
        ]

        return Response({
            "count": total,
            "next": None if (start + page_size) >= total else f"?page={page + 1}",
            "previous": None if page <= 1 else f"?page={page - 1}",
            "results": results,
        })


class LeadStatsView(APIView):
    """Lead analytics — totals, recent counts, and trend."""
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        from apps.core.models import Lead

        now = timezone.now()
        total = Lead.objects.count()
        this_month = Lead.objects.filter(created_at__gte=now.replace(day=1, hour=0, minute=0, second=0)).count()
        last_30 = Lead.objects.filter(created_at__gte=now - timedelta(days=30)).count()
        last_7 = Lead.objects.filter(created_at__gte=now - timedelta(days=7)).count()
        source_breakdown = list(
            Lead.objects.exclude(lead_source="")
            .values("lead_source")
            .annotate(count=Count("id"))
            .order_by("-count", "lead_source")[:6]
        )

        # Monthly trend for last 6 months
        trend = []
        for i in range(5, -1, -1):
            month_start = (now.replace(day=1) - timedelta(days=30 * i)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if i > 0:
                month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
            else:
                month_end = now
            count = Lead.objects.filter(created_at__gte=month_start, created_at__lt=month_end).count()
            trend.append({
                "month": month_start.strftime("%b %Y"),
                "count": count,
            })

        return Response({
            "total": total,
            "this_month": this_month,
            "last_30_days": last_30,
            "last_7_days": last_7,
            "contacted": Lead.objects.filter(is_contacted=True).count(),
            "not_contacted": Lead.objects.filter(is_contacted=False).count(),
            "source_breakdown": source_breakdown,
            "trend": trend,
        })


class LeadDetailView(APIView):
    """Update a single lead (e.g., toggle contacted status)."""
    permission_classes = SUPERADMIN_PERMS

    def patch(self, request, pk):
        _ensure_public()
        from apps.core.models import Lead

        try:
            lead = Lead.objects.get(pk=pk)
        except Lead.DoesNotExist:
            return Response({"detail": "Lead not found."}, status=404)

        if "is_contacted" in request.data:
            lead.is_contacted = bool(request.data["is_contacted"])
            if lead.is_contacted and not lead.contacted_at:
                lead.contacted_at = timezone.now()
            elif not lead.is_contacted:
                lead.contacted_at = None
            lead.save(update_fields=["is_contacted", "contacted_at"])

        return Response({
            "id": lead.id,
            "name": lead.name,
            "email": lead.email,
            "phone": lead.phone,
            "company_name": lead.company_name,
            "lead_source": lead.lead_source,
            "referral_name": lead.referral_name,
            "message": lead.message,
            "is_contacted": lead.is_contacted,
            "contacted_at": lead.contacted_at.isoformat() if lead.contacted_at else None,
            "created_at": lead.created_at.isoformat(),
        })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SMS OVERVIEW (Superadmin)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class SuperadminSMSOverviewView(APIView):
    """
    GET /api/v1/superadmin/sms/overview/
    Returns all tenants using inbuilt SMS, their wallet balances,
    SMS topup payment history, and the master Bytewave provider balance.
    """
    permission_classes = SUPERADMIN_PERMS

    def get(self, request):
        _ensure_public()
        tenants = (
            Tenant.objects.select_related("company")
            .exclude(schema_name__in=PROTECTED_SCHEMAS)
            .filter(is_active=True)
        )

        tenant_rows = []
        total_inbuilt_units = Decimal("0.00")

        for tenant in tenants:
            try:
                with schema_context(tenant.schema_name):
                    from apps.messaging.models import SMSGatewayConfig, TenantSMSWallet, SMSUnitTopup

                    gateway = SMSGatewayConfig.objects.filter(
                        is_active=True, use_inbuilt_system=True
                    ).first()

                    if not gateway:
                        continue  # skip tenants not using inbuilt

                    wallet = TenantSMSWallet.objects.filter(is_active=True).first()
                    units = wallet.sms_units if wallet else Decimal("0.00")
                    total_inbuilt_units += units

                    # Recent topups
                    topups = list(
                        SMSUnitTopup.objects.order_by("-created_at")[:5].values(
                            "id", "units_purchased", "amount_paid",
                            "status", "payment_method", "created_at",
                        )
                    )
                    for t in topups:
                        t["created_at"] = t["created_at"].isoformat()
                        t["amount_paid"] = str(t["amount_paid"])

                    tenant_rows.append({
                        "tenant_id": str(tenant.id),
                        "tenant_name": tenant.company.name if tenant.company else tenant.subdomain,
                        "tenant_subdomain": tenant.subdomain,
                        "sms_units": str(units),
                        "sell_price_per_unit": str(wallet.sell_price_per_unit) if wallet else "0.40",
                        "recent_topups": topups,
                    })
            except Exception as exc:
                logger.warning("SMS overview error for %s: %s", tenant.schema_name, exc)

        # Fetch master Bytewave balance
        provider_balance = self._get_bytewave_balance()

        # All topup history across tenants (for the payments tab)
        all_topups = self._get_all_topup_history(tenants)

        return Response({
            "total_inbuilt_units": str(total_inbuilt_units),
            "inbuilt_tenant_count": len(tenant_rows),
            "provider_balance": provider_balance,
            "tenants": tenant_rows,
            "all_topups": all_topups,
        })

    def _get_bytewave_balance(self) -> dict:
        import requests as _requests
        from django.conf import settings as _settings

        api_token = getattr(_settings, "BYTEWAVE_API_TOKEN", "")
        base_url = "https://portal.bytewavenetworks.com/api/http"

        if not api_token:
            return {"success": False, "error": "BYTEWAVE_API_TOKEN not configured", "balance": 0}

        try:
            resp = _requests.get(
                f"{base_url}/balance",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={"api_token": api_token},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("status") == "success":
                raw = data.get("data", {})
                # data can be dict or scalar depending on Bytewave version
                if isinstance(raw, dict):
                    units = (
                        raw.get("sms_unit")
                        or raw.get("sms_units")
                        or raw.get("units")
                        or raw.get("balance")
                        or raw.get("remaining")
                        or 0
                    )
                elif isinstance(raw, (int, float, str)):
                    units = raw
                else:
                    units = 0

                return {
                    "success": True,
                    "balance": float(units),
                    "currency": "SMS_UNITS",
                    "raw": raw,  # include raw so you can debug what fields come back
                }

            return {
                "success": False,
                "error": data.get("message", "Unknown error from Bytewave"),
                "balance": 0,
            }

        except Exception as exc:
            logger.error("Bytewave balance fetch failed: %s", exc)
            return {"success": False, "error": str(exc), "balance": 0}

    def _get_all_topup_history(self, tenants, limit_per_tenant: int = 50) -> list:
        rows = []
        for tenant in tenants:
            try:
                with schema_context(tenant.schema_name):
                    from apps.messaging.models import SMSUnitTopup, SMSGatewayConfig

                    if not SMSGatewayConfig.objects.filter(
                        is_active=True, use_inbuilt_system=True
                    ).exists():
                        continue

                    for t in SMSUnitTopup.objects.order_by("-created_at")[:limit_per_tenant].values(
                        "id", "units_purchased", "amount_paid",
                        "status", "payment_method", "payment_reference",
                        "checkout_request_id", "created_at",
                    ):
                        rows.append({
                            **t,
                            "created_at": t["created_at"].isoformat(),
                            "amount_paid": str(t["amount_paid"]),
                            "tenant_name": tenant.company.name if tenant.company else tenant.subdomain,
                            "tenant_subdomain": tenant.subdomain,
                        })
            except Exception as exc:
                logger.warning("Topup history error for %s: %s", tenant.schema_name, exc)

        rows.sort(key=lambda x: x["created_at"], reverse=True)
        return rows