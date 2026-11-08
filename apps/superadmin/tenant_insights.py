"""
Tenant insights — one read-only, cached endpoint that powers the redesigned
superadmin tenant detail page.

GET /api/v1/superadmin/tenants/<uuid>/insights/[?refresh=1]

Cost: 1 grouped query on billing_payment + 2 small aggregates in the tenant
schema, ~5 small queries in public. Result cached 120s per tenant.
"""
import logging
from datetime import date, datetime, time

from django.core.cache import cache
from django.db import connection
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import Tenant
from apps.subscriptions.models import BillingCycle, CompanySubscription, SubscriptionPayment
from apps.subscriptions.system_accounts import SYSTEM_BILLING_CUSTOMER_CODE

from .perf import cycle_metrics
from .permissions import IsSuperAdmin

logger = logging.getLogger(__name__)

PROTECTED_SCHEMAS = {"public", "information_schema", "pg_catalog", "pg_toast"}
CACHE_TTL = 120


# ───────────────────────── helpers ─────────────────────────

def _month_start(today: date, offset: int) -> date:
    idx = today.year * 12 + (today.month - 1) + offset
    return date(idx // 12, idx % 12 + 1, 1)


def _aware(d: date) -> datetime:
    return timezone.make_aware(datetime.combine(d, time.min))


def _pct(new, old):
    if old in (None, 0) or new is None:
        return None
    return round((new - old) / old * 100, 1)


def _f(v) -> float:
    return round(float(v or 0), 2)


def _linreg(ys):
    """Least squares on x=0..n-1. Returns (slope, intercept, r2)."""
    n = len(ys)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0), 0.0
    mx, my = (n - 1) / 2, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in range(n))
    sxy = sum((x - mx) * (y - my) for x, y in zip(range(n), ys))
    slope = sxy / sxx if sxx else 0.0
    icpt = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (icpt + slope * x)) ** 2 for x, y in zip(range(n), ys))
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return slope, icpt, max(0.0, min(r2, 1.0))


# ───────────────────── tenant-schema metrics ─────────────────────

def _tenant_metrics(tenant, months):
    """Revenue by month/service + subscriber counts. Never raises."""
    buckets = {(m.year, m.month): {"pppoe": 0.0, "hotspot": 0.0, "other": 0.0} for m in months}
    subs = {"active": 0, "total": 0, "new_month": 0, "base_year_ago": 0, "terminated_month": 0}
    try:
        with schema_context(tenant.schema_name):
            from apps.billing.models import Payment
            from apps.customers.models import Customer, ServiceConnection

            rows = (
                Payment.objects
                .filter(status="COMPLETED", payment_date__gte=_aware(months[0]))
                .annotate(m=TruncMonth("payment_date"))
                .values("m", "service_type")
                .annotate(t=Sum("amount"))
                .order_by()
            )
            for r in rows:
                b = buckets.get((r["m"].year, r["m"].month))
                if b is None:
                    continue
                st = (r["service_type"] or "").upper()
                key = "hotspot" if st == "HOTSPOT" else "pppoe" if st == "PPPOE" else "other"
                b[key] += float(r["t"] or 0)

            month_start, year_ago = _aware(months[-1]), _aware(months[0])
            c = (
                Customer.objects.exclude(customer_code=SYSTEM_BILLING_CUSTOMER_CODE)
                .aggregate(
                    total=Count("id"),
                    active=Count("id", filter=Q(status="ACTIVE")),
                    new_month=Count("id", filter=Q(created_at__gte=month_start)),
                    base=Count("id", filter=Q(created_at__lt=year_ago)),
                )
            )
            s = ServiceConnection.objects.aggregate(
                terminated=Count("id", filter=Q(termination_date__gte=month_start)),
            )
            subs = {
                "active": c["active"] or 0,
                "total": c["total"] or 0,
                "new_month": c["new_month"] or 0,
                "base_year_ago": c["base"] or 0,
                "terminated_month": s["terminated"] or 0,
            }
    except Exception:
        logger.warning("tenant insights: schema metrics failed for %s", tenant.schema_name, exc_info=True)
    return buckets, subs


# ───────────────────────── builders ─────────────────────────

def _projection(series):
    """series: 13 months, last = current (partial). Fit on the 6 complete months before it."""
    totals = [m["total"] for m in series]
    fit = totals[6:12]
    if sum(1 for v in fit if v > 0) < 3:
        return {"actual": [], "projected": [], "next_month_forecast": None, "confidence": 0}
    slope, icpt, r2 = _linreg(fit)
    actual = [{"label": m["label"], "value": m["total"]} for m in series[-4:]]
    projected, last = [], series[-1]["month_date"]
    for step in range(1, 6):
        x = 6 + step                      # fit index 0..5 = months -6..-1; current = 6
        mo = _month_start(last, step)
        projected.append({"label": mo.strftime("%b"), "value": round(max(0.0, icpt + slope * x), 2)})
    return {
        "actual": actual,
        "projected": projected,
        "next_month_forecast": projected[0]["value"] if projected else None,
        "forecast_label": projected[-1]["label"] if projected else None,
        "forecast_value": projected[-1]["value"] if projected else None,
        "confidence": int(round(r2 * 100)),
    }


def _signal(yoy, sub_growth, churn, streak):
    def part(v, cap):
        return 0.0 if v is None else max(0.0, min(v / cap, 1.0))

    score = (
        40 * part(yoy, 50) + 30 * part(sub_growth, 40)
        + 20 * (1 - min((churn or 0) / 10, 1)) + 10 * part(streak, 8)
    )
    score = int(round(score))
    if score >= 70:
        label, badge = "Scaling consistently", "STRONG"
    elif score >= 45:
        label, badge = "Holding steady", "STABLE"
    else:
        label, badge = "Needs attention", "WEAK"
    if streak >= 2:
        summary = f"Revenue has grown for {streak} consecutive months" + (
            " with subscriber growth supporting the trend." if sub_growth and sub_growth > 0 else "."
        )
    elif yoy is None:
        summary = "Not enough billing history yet to score growth."
    else:
        summary = "Revenue growth has been uneven over recent months."
    return {"score": score, "label": label, "badge": badge, "summary": summary}


def _subscription_block(tenant, month_revenue):
    block = {"exists": False, "payments": []}
    with schema_context(get_public_schema_name()):
        sub = (
            CompanySubscription.objects.select_related("plan")
            .filter(company_id=tenant.company_id).first()
        )
        if not sub:
            return block

        plan = sub.plan
        price = float(sub.current_price or 0)
        if plan and plan.is_metered:
            cycle = (
                BillingCycle.objects.filter(tenant=tenant, subscription=sub)
                .select_related("tenant", "subscription__plan")
                .order_by("-start_date").first()
            )
            if cycle:
                try:
                    price = float(cycle_metrics(cycle)["total"])
                except Exception:
                    pass

        paid = SubscriptionPayment.objects.filter(subscription=sub, status="completed").aggregate(
            total=Sum("amount"), n=Count("id"))
        cyc = BillingCycle.objects.filter(tenant=tenant).aggregate(
            paid=Count("id", filter=Q(status="paid")),
            overdue=Count("id", filter=Q(status="invoiced", grace_ends_at__lt=timezone.now())),
        )
        denom = (cyc["paid"] or 0) + (cyc["overdue"] or 0)

        pays = (
            SubscriptionPayment.objects
            .filter(subscription=sub, status="completed", completed_at__isnull=False)
            .select_related("intended_plan")
            .order_by("-completed_at")[:8]
        )
        max_subs = plan.max_subscribers if plan else 0
        block.update({
            "exists": True,
            "plan_name": plan.name if plan else "—",
            "plan_description": (plan.tagline or plan.description) if plan else "",
            "is_metered": bool(plan and plan.is_metered),
            "status": sub.status,
            "billing_period": sub.billing_period,
            "price": round(price, 2),
            "max_subscribers": max_subs,          # 0 = unlimited
            "next_billing": sub.current_period_end.isoformat() if sub.current_period_end else None,
            "trial_ends_at": sub.trial_ends_at.isoformat() if sub.trial_ends_at else None,
            "lifetime_paid": _f(paid["total"]),
            "payments_count": paid["n"] or 0,
            "reliability_pct": round((cyc["paid"] or 0) / denom * 100) if denom else None,
            "revenue_multiple": round(month_revenue / price, 1) if price > 0 else None,
            "payments": [{
                "id": str(p.id),
                "reference": p.mpesa_receipt or p.bank_reference or p.payhero_reference or "—",
                "plan_name": (p.intended_plan.name if p.intended_plan else (plan.name if plan else "—")),
                "period_start": p.period_start.isoformat() if p.period_start else None,
                "period_end": p.period_end.isoformat() if p.period_end else None,
                "paid_on": p.completed_at.isoformat(),
                "amount": _f(p.amount),
                "status": p.status,
            } for p in pays],
        })
    return block


def build_tenant_insights(tenant) -> dict:
    now = timezone.localtime()
    today = now.date()
    months = [_month_start(today, -12 + i) for i in range(13)]   # [-12 … current]

    buckets, subs = _tenant_metrics(tenant, months)

    series = []
    for m in months:
        b = buckets[(m.year, m.month)]
        series.append({
            "key": m.strftime("%Y-%m"), "label": m.strftime("%b"),
            "pppoe": round(b["pppoe"], 2), "hotspot": round(b["hotspot"], 2),
            "other": round(b["other"], 2),
            "total": round(b["pppoe"] + b["hotspot"] + b["other"], 2),
        })

    cur, prev, first = series[-1], series[-2], series[0]
    yoy = _pct(cur["total"], first["total"])

    streak = 0
    for i in range(11, 0, -1):            # complete months only
        if series[i]["total"] > series[i - 1]["total"] > 0:
            streak += 1
        else:
            break

    active = subs["active"]
    sub_growth = _pct(subs["total"], subs["base_year_ago"])
    churn_base = active + subs["terminated_month"]
    churn = round(subs["terminated_month"] / churn_base * 100, 1) if churn_base else None
    arpu = round(cur["total"] / active, 2) if active else 0.0
    prev_subs = max(active - subs["new_month"], 0)
    arpu_prev = (prev["total"] / prev_subs) if prev_subs else 0
    arpu_change = _pct(arpu, arpu_prev) if arpu_prev else None

    sub_block = _subscription_block(tenant, cur["total"])
    company = tenant.company
    s_status = sub_block.get("status")
    if tenant.status in ("suspended", "cancelled") or s_status in ("past_due", "expired", "cancelled"):
        health = "at_risk"
    elif s_status == "trialing" or tenant.status == "trial":
        health = "watch"
    else:
        health = "healthy"

    projection = _projection([{**m, "month_date": months[i]} for i, m in enumerate(series)])

    total_now = cur["total"]
    return {
        "currency": getattr(company, "base_currency", None) or "KES",
        "generated_at": now.isoformat(),
        "tenant": {
            "id": str(tenant.id),
            "code": f"TEN-{tenant.pk.hex[:6].upper()}",
            "company_name": company.name,
            "subdomain": tenant.subdomain,
            "status": tenant.status,
            "health": health,
            "joined_at": tenant.created_at.isoformat() if tenant.created_at else None,
            "location": ", ".join(filter(None, [company.city, company.county])) or "",
            "website": company.website or "",
            "active_subscribers": active,
        },
        "revenue": {
            "current_month": total_now,
            "previous_month": prev["total"],
            "mom_pct": _pct(total_now, prev["total"]),
            "pppoe": cur["pppoe"],
            "hotspot": cur["hotspot"],
            "other": cur["other"],
            "pppoe_share": round(cur["pppoe"] / total_now * 100, 1) if total_now else 0,
            "hotspot_share": round(cur["hotspot"] / total_now * 100, 1) if total_now else 0,
            "pppoe_mom_pct": _pct(cur["pppoe"], prev["pppoe"]),
            "hotspot_mom_pct": _pct(cur["hotspot"], prev["hotspot"]),
            "run_rate": round(total_now * 12, 2),
            "yoy_pct": yoy,
        },
        "monthly": series[1:],   # 12 points, last = current month
        "signal": {
            **_signal(yoy, sub_growth, churn, streak),
            "revenue_growth_yoy": yoy,
            "subscriber_growth_yoy": sub_growth,
            "arpu_change_pct": arpu_change,
            "churn_rate": churn,
            "growth_streak": streak,
        },
        "projection": projection,
        "subscription": sub_block,
        "stats": {
            "active_subscribers": active,
            "new_this_month": subs["new_month"],
            "arpu": arpu,
            "arpu_change_pct": arpu_change,
            "churn_rate": churn,
        },
    }


class TenantInsightsView(APIView):
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request, pk):
        connection.set_schema_to_public()
        tenant = get_object_or_404(
            Tenant.objects.select_related("company").exclude(schema_name__in=PROTECTED_SCHEMAS),
            pk=pk,
        )
        key = f"sa:tenant_insights:v1:{tenant.pk}"
        if request.query_params.get("refresh") != "1":
            cached = cache.get(key)
            if cached is not None:
                return Response(cached)
        data = build_tenant_insights(tenant)
        cache.set(key, data, CACHE_TTL)
        return Response(data)