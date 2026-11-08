"""
Superadmin performance helpers
──────────────────────────────
Shared, cache-aware utilities for the superadmin dashboard and tenant list.

Two responsibilities:

1. `build_tenant_maps()` — preloads CompanySubscription and latest-BillingCycle
   for every tenant in 2 queries, so list endpoints don't issue N queries.
2. `cycle_metrics(cycle)` — computes (raw PPPoE count, billed count, PPPoE charge,
   hotspot share, total) once per cycle and caches for 5 minutes.
3. `run_across_schemas(template, schemas, params)` — packs cross-schema
   aggregate queries into a few UNION ALL round-trips instead of one per tenant.
   Uses fully-qualified "{schema}"."table" names — never touches search_path.
"""

import logging
import re
from decimal import Decimal

from django.core.cache import cache
from django.db import connection

from apps.subscriptions.models import BillingCycle, CompanySubscription

logger = logging.getLogger(__name__)

# Only identifier-shaped schemas are allowed into SQL. Tenant schemas are
# created from sanitized subdomains (see TenantCreateView: replace('-','_')),
# so this regex covers every legitimate schema and rejects anything else.
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# How many schemas to pack into one UNION ALL. 40 keeps the SQL short enough
# that planner cost stays predictable, and matches the spec's chosen value.
_CHUNK = 40

# Per-cycle metric cache TTL. Matches the spec.
CYCLE_METRICS_TTL = 300


def build_tenant_maps():
    """
    Two queries total, instead of two-plus per tenant.

    Returns:
        (sub_map, cycle_map)
        sub_map:   {company_id: CompanySubscription}
        cycle_map: {tenant_id: BillingCycle}   # latest per tenant
    """
    sub_map = {
        s.company_id: s
        for s in CompanySubscription.objects.select_related("plan")
    }
    cycle_map = {
        c.tenant_id: c
        for c in (
            BillingCycle.objects
            .select_related("tenant", "subscription__plan")
            .order_by("tenant_id", "-start_date", "-end_date")
            .distinct("tenant_id")
        )
    }
    return sub_map, cycle_map


def cycle_metrics(cycle):
    """
    One RadAcct scan per cycle (cached) instead of 3–4 per request.

    Returns:
        {
            "raw":           int,        # raw PPPoE usernames currently billable
            "billed":        int,        # confirmed billable count
            "pppoe_charge":  Decimal,    # count * snapshot_pppoe_price
            "hotspot_share": Decimal,    # hotspot revenue share amount
            "total":         Decimal,    # max(pppoe_charge + share, monthly minimum)
        }

    For non-metered plans returns zeros for the usage pieces and the plan's
    subscription price as `total` — matches how the dashboard treats fixed-
    price plans.
    """
    if cycle is None:
        return {
            "raw": 0,
            "billed": 0,
            "pppoe_charge": Decimal("0.00"),
            "hotspot_share": Decimal("0.00"),
            "total": Decimal("0.00"),
        }

    key = f"sa:cycle:{cycle.pk}:m"
    cached = cache.get(key)
    if cached is not None:
        return cached

    subscription = getattr(cycle, "subscription", None)
    plan = getattr(subscription, "plan", None) if subscription else None

    if plan is not None and not getattr(plan, "is_metered", False):
        total = getattr(subscription, "current_price", Decimal("0.00")) or Decimal("0.00")
        data = {
            "raw": 0,
            "billed": 0,
            "pppoe_charge": Decimal("0.00"),
            "hotspot_share": Decimal("0.00"),
            "total": total,
        }
    else:
        try:
            count = len(cycle.get_billable_pppoe_usernames())
        except Exception:
            logger.warning(
                "cycle_metrics usage scan failed for %s", cycle.pk, exc_info=True,
            )
            count = 0
        price = cycle.snapshot_pppoe_price or Decimal("0.00")
        pppoe_charge = (Decimal(count) * price).quantize(Decimal("0.01"))
        try:
            share = cycle.calculate_hotspot_revenue_share()
        except Exception:
            logger.warning(
                "cycle_metrics hotspot share failed for %s", cycle.pk, exc_info=True,
            )
            share = Decimal("0.00")
        minimum = cycle.snapshot_base_fee or Decimal("500.00")
        total = max(pppoe_charge + share, minimum).quantize(Decimal("0.01"))
        data = {
            "raw": count,
            "billed": count,
            "pppoe_charge": pppoe_charge,
            "hotspot_share": share,
            "total": total,
        }

    cache.set(key, data, CYCLE_METRICS_TTL)
    return data


def run_across_schemas(template, schemas, params=()):
    """
    Run one schema-qualified query ({schema} placeholder) over many tenants
    using UNION ALL: one round trip per chunk instead of one per tenant.

    Args:
        template: str with a single `{schema}` placeholder. Must be a SELECT.
        schemas:  iterable of schema names (tenant.schema_name values)
        params:   tuple of bind parameters — MUST be the same count for every
                  schema in the query. Each schema's UNION leg gets
                  `params * len(chunk)` substituted in order.

    Returns:
        list of rows (list of tuples), concatenated across schemas.

    Falls back to per-schema execution if a chunk fails (e.g. a tenant schema
    is missing the table). Never touches search_path, so the caller's schema
    context is preserved.
    """
    # Silently drop anything that could break SQL injection safety.
    clean_schemas = [s for s in schemas if s and _SCHEMA_RE.match(s)]
    if len(clean_schemas) != len(schemas):
        bad = [s for s in schemas if s not in clean_schemas]
        logger.warning(
            "run_across_schemas: dropped %d non-identifier schema(s): %r",
            len(bad), bad[:5],
        )

    params = tuple(params)
    rows = []

    def _run(chunk):
        sql = " UNION ALL ".join(template.format(schema=s) for s in chunk)
        with connection.cursor() as cur:
            cur.execute(sql, params * len(chunk))
            return cur.fetchall()

    for i in range(0, len(clean_schemas), _CHUNK):
        chunk = clean_schemas[i:i + _CHUNK]
        try:
            rows.extend(_run(chunk))
        except Exception:
            logger.warning(
                "run_across_schemas: chunk of %d failed, falling back per-schema",
                len(chunk), exc_info=True,
            )
            for s in chunk:
                try:
                    rows.extend(_run([s]))
                except Exception as exc:
                    logger.warning("cross-schema query skipped %s: %s", s, exc)
    return rows