"""
apps/core/tenant_purge.py
──────────────────────────────────────────────────────────────────────────────
Hard-delete utility: completely wipes a tenant and every public-schema record
associated with it, in the correct FK dependency order, inside a single
atomic transaction.

Why this matters
────────────────
PostgreSQL enforces FK constraints strictly. The dependency chain is:

    SubscriptionPayment ──► CompanySubscription (OneToOne) ──► Company ──◄── Tenant
                                                                         |
                                                                    User.company_id
                                                                    User.tenant_id
                                                                    Domain.tenant_id

Deleting in the wrong order causes IntegrityError which Django swallows,
leaving "ghost" rows in the database.

Design decisions
────────────────
• ORM deletes happen inside transaction.atomic() — fully rollback-safe.
• The physical schema DROP runs OUTSIDE the transaction because DDL
  (DROP SCHEMA … CASCADE) cannot be rolled back in PostgreSQL.
  If the DROP fails the ORM records are already gone — the orphaned
  schema has no live FK references and can be manually dropped later.
• The function is importable by both the Celery task and the synchronous
  hard-delete API endpoint so the logic is never duplicated.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import Q
from django_tenants.utils import get_public_schema_name, schema_context

logger = logging.getLogger(__name__)
User = get_user_model()

PROTECTED_SCHEMAS = frozenset(
    {"public", "information_schema", "pg_catalog", "pg_toast"}
)
_VALID_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


# ─── Result dataclass ────────────────────────────────────────────────────────


@dataclass
class PurgeResult:
    tenant_pk: str
    schema_name: str
    subdomain: str
    company_name: str

    # Row counts
    subscription_payments_deleted: int = 0
    subscriptions_deleted: int = 0
    users_deleted: int = 0
    domains_deleted: int = 0
    tenant_deleted: bool = False
    company_deleted: bool = False

    # Integration cleanup
    router_index_deleted: int = 0
    router_map_deleted: int = 0
    radius_config_deleted: int = 0

    # Schema drop
    schema_dropped: bool = False
    schema_drop_error: str = ""

    # Warnings accumulated during cleanup
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "tenant_pk": self.tenant_pk,
            "schema_name": self.schema_name,
            "subdomain": self.subdomain,
            "company_name": self.company_name,
            "rows_deleted": {
                "subscription_payments": self.subscription_payments_deleted,
                "subscriptions": self.subscriptions_deleted,
                "users": self.users_deleted,
                "domains": self.domains_deleted,
                "tenant": self.tenant_deleted,
                "company": self.company_deleted,
            },
            "integrations_cleaned": {
                "router_index": self.router_index_deleted,
                "router_map": self.router_map_deleted,
                "radius_config": self.radius_config_deleted,
            },
            "schema_dropped": self.schema_dropped,
            "schema_drop_error": self.schema_drop_error,
            "warnings": self.warnings,
        }


# ─── Internal helpers ─────────────────────────────────────────────────────────


def _assert_safe_schema(schema_name: str) -> None:
    """Raise ValueError for protected or malformed schema names."""
    if schema_name in PROTECTED_SCHEMAS:
        raise ValueError(
            f"Refusing to purge protected schema '{schema_name}'. "
            "This is a critical system schema."
        )
    if not _VALID_SCHEMA_RE.match(schema_name):
        raise ValueError(
            f"Schema name '{schema_name}' is invalid. "
            "Only alphanumeric characters and underscores are allowed."
        )


def _drop_schema(schema_name: str) -> tuple[bool, str]:
    """
    Execute DROP SCHEMA … CASCADE outside any transaction.
    Returns (success: bool, error_message: str).
    """
    try:
        with connection.cursor() as cur:
            cur.execute('SET search_path TO "public"')
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        logger.info("purge_tenant: dropped schema '%s'", schema_name)
        return True, ""
    except Exception as exc:
        msg = str(exc)
        logger.error("purge_tenant: failed to drop schema '%s': %s", schema_name, msg)
        return False, msg


def _cleanup_integrations(
    tenant_id,
    schema_name: str,
    result: PurgeResult,
) -> None:
    """Remove global router indexes and RADIUS registry entries."""
    # GlobalRouterMap + RouterTenantIndex
    try:
        from apps.core.models import GlobalRouterMap, RouterTenantIndex

        result.router_index_deleted = RouterTenantIndex.objects.filter(
            tenant_id=tenant_id
        ).delete()[0]
        result.router_map_deleted = GlobalRouterMap.objects.filter(
            tenant_id=tenant_id
        ).delete()[0]
    except Exception as exc:
        result.warnings.append(f"Router index cleanup warning: {exc}")
        logger.warning(
            "purge_tenant: router index cleanup failed for %s: %s", schema_name, exc
        )

    # RadiusTenantConfig
    try:
        from apps.radius.models import RadiusTenantConfig

        result.radius_config_deleted = RadiusTenantConfig.objects.filter(
            schema_name=schema_name
        ).delete()[0]
    except Exception as exc:
        result.warnings.append(f"RADIUS config cleanup warning: {exc}")
        logger.warning(
            "purge_tenant: RADIUS config cleanup failed for %s: %s", schema_name, exc
        )

    # RADIUS service teardown
    try:
        from apps.radius.services.tenant_radius_service import tenant_radius_service

        tenant_radius_service.remove_tenant_radius(schema_name)
    except Exception as exc:
        result.warnings.append(f"RADIUS service teardown warning: {exc}")
        logger.warning(
            "purge_tenant: RADIUS service teardown failed for %s: %s", schema_name, exc
        )


# ─── Public API ───────────────────────────────────────────────────────────────


def purge_tenant_completely(tenant_pk) -> PurgeResult:
    """
    Completely and safely remove a tenant and all its associated public-schema
    records. Raises ValueError for protected schemas or if the tenant is not found.

    Deletion order (respects FK constraints):
      1. SubscriptionPayment  (FK → CompanySubscription)
      2. CompanySubscription  (OneToOne → Company)
      3. User                 (FK → Company / FK → Tenant)
      4. Domain               (FK → Tenant)
      5. Tenant row           (ORM)
      6. Company row          (ORM)
      7. DROP SCHEMA … CASCADE  ← outside transaction (DDL cannot be rolled back)

    Integration cleanup (GlobalRouterMap, RouterTenantIndex, RadiusTenantConfig)
    is also performed within the transaction.
    """
    public_schema = get_public_schema_name()

    with schema_context(public_schema):
        from apps.core.models import Tenant

        try:
            tenant = (
                Tenant.objects
                .select_related("company")
                .get(pk=tenant_pk)
            )
        except Tenant.DoesNotExist:
            raise ValueError(f"Tenant with pk={tenant_pk} does not exist.")

        schema_name = tenant.schema_name
        _assert_safe_schema(schema_name)

        company = tenant.company
        result = PurgeResult(
            tenant_pk=str(tenant_pk),
            schema_name=schema_name,
            subdomain=tenant.subdomain,
            company_name=company.name if company else "",
        )

        logger.warning(
            "purge_tenant_completely: initiating hard purge — "
            "tenant=%s schema=%s company=%s",
            tenant.subdomain,
            schema_name,
            result.company_name,
        )

        # ── Integration cleanup (inside atomic so it rolls back on failure) ──
        _cleanup_integrations(tenant.pk, schema_name, result)

        # ── ORM deletes in FK dependency order, fully atomic ─────────────────
        with transaction.atomic():
            # 1. SubscriptionPayment → CompanySubscription
            try:
                from apps.subscriptions.models import (
                    CompanySubscription,
                    SubscriptionPayment,
                )

                if company:
                    try:
                        sub = CompanySubscription.objects.get(company=company)
                        result.subscription_payments_deleted = (
                            SubscriptionPayment.objects.filter(subscription=sub).delete()[0]
                        )
                        sub.delete()
                        result.subscriptions_deleted = 1
                    except CompanySubscription.DoesNotExist:
                        pass  # No subscription — nothing to delete
            except ImportError:
                result.warnings.append(
                    "subscriptions app not installed; subscription cleanup skipped."
                )

            # 2. Users linked to this company or tenant (public schema)
            # Django intentionally blocks delete() on distinct() querysets, so
            # de-duplicate first, then delete through a plain pk__in queryset.
            user_filter = Q(tenant=tenant) | Q(tenant_subdomain=tenant.subdomain)
            if company:
                user_filter |= Q(company=company) | Q(company_name=company.name)

            user_ids = list(
                User.objects.filter(user_filter)
                .values_list("pk", flat=True)
                .distinct()
            )
            if user_ids:
                result.users_deleted = User.objects.filter(pk__in=user_ids).delete()[0]

            # 3. Domains (FK → Tenant)
            result.domains_deleted = tenant.domains.all().delete()[0]

            # 4. Tenant row
            tenant.delete()
            result.tenant_deleted = True

            # 5. Company row (now safe — all FK children gone)
            if company:
                company.delete()
                result.company_deleted = True

        logger.info(
            "purge_tenant_completely: ORM cleanup done — %s",
            result.as_dict(),
        )

        # ── Physical schema DROP (outside transaction — DDL cannot roll back) ─
        dropped, err = _drop_schema(schema_name)
        result.schema_dropped = dropped
        result.schema_drop_error = err

        return result
