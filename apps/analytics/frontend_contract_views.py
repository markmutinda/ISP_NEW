# apps/analytics/frontend_contract_views.py
from datetime import timedelta, datetime
from dateutil.relativedelta import relativedelta

from django.core.cache import cache
from django.db import connection
from django.db import models
from django.db.models import Sum, Count, Avg, Q, F, DecimalField, ExpressionWrapper, BigIntegerField
from django.db.models.functions import Coalesce, TruncDay, TruncHour, TruncMonth, ExtractHour, ExtractWeekDay
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsAdminOrStaff
from apps.billing.models.payment_models import Payment
from apps.customers.models import Customer, ServiceConnection
from apps.billing.models.billing_models import Plan
from apps.bandwidth.models import DataUsage


CACHE_TTL = 300  # 5 min

# ==========================================
# PAYMENT METHOD NORMALIZATION
# ==========================================

PAYMENT_METHOD_NORMALIZATION = {
    "mpesa": "M-Pesa",
    "m-pesa": "M-Pesa",
    "card": "Card",
    "visa": "Card",
    "mastercard": "Card",
    "bank": "Bank Transfer",
    "bank transfer": "Bank Transfer",
    "cash": "Cash",
}


def _norm_method(name: str) -> str:
    """Normalize payment method names for consistent reporting."""
    n = (name or "").strip().lower()
    for k, v in PAYMENT_METHOD_NORMALIZATION.items():
        if k in n:
            return v
    return "Other"


def _pct(cur, prev):
    """Calculate percentage change between current and previous values."""
    if not prev:
        return 0.0
    return round(((cur - prev) / prev) * 100, 2)


def _safe_float(v):
    """Safely convert value to float, defaulting to 0."""
    return float(v or 0)


def _bytes_to_tb(v):
    """Convert bytes to terabytes (4 decimal places)."""
    return round(_safe_float(v) / (1024 ** 4), 4)


def _bytes_to_gb(v):
    """Convert bytes to gigabytes (2 decimal places)."""
    return round(_safe_float(v) / (1024 ** 3), 2)


def _get_top_impactful_customers():
    """Top 10 customers by lifetime spend. Combines PPPoE + Hotspot clients."""
    from apps.billing.models.payment_models import Payment
    from django.db.models import Sum, Count, Q

    # PPPoE customers
    pppoe_rows = (
        Payment.objects
        .filter(status__iexact="completed", customer__isnull=False)
        .values("customer_id", "customer__user__first_name", "customer__user__last_name", "customer__customer_code")
        .annotate(total=Sum("amount"), tx_count=Count("id"))
        .order_by("-total")
    )

    # Hotspot clients
    hotspot_rows = (
        Payment.objects
        .filter(status__iexact="completed", hotspot_session__hotspot_client__isnull=False)
        .values(
            "hotspot_session__hotspot_client__id",
            "hotspot_session__hotspot_client__canonical_username",
            "hotspot_session__hotspot_client__canonical_phone",
        )
        .annotate(total=Sum("amount"), tx_count=Count("id"))
        .order_by("-total")
    )

    bucket = []

    for r in pppoe_rows:
        fn = r.get("customer__user__first_name") or ""
        ln = r.get("customer__user__last_name") or ""
        name = f"{fn} {ln}".strip() or r.get("customer__customer_code") or "Unknown"
        bucket.append({
            "type": "PPPOE",
            "display_name": name,
            "identifier": r.get("customer__customer_code") or "",
            "total_amount": _safe_float(r["total"]),
            "tx_count": r["tx_count"],
        })

    for r in hotspot_rows:
        bucket.append({
            "type": "HOTSPOT",
            "display_name": r.get("hotspot_session__hotspot_client__canonical_username") or "Hotspot User",
            "identifier": r.get("hotspot_session__hotspot_client__canonical_phone") or "",
            "total_amount": _safe_float(r["total"]),
            "tx_count": r["tx_count"],
        })

    # Merge & deduplicate by display_name, sort, top 10
    seen = {}
    for item in bucket:
        key = item["display_name"]
        if key in seen:
            seen[key]["total_amount"] += item["total_amount"]
            seen[key]["tx_count"] += item["tx_count"]
        else:
            seen[key] = dict(item)

    return sorted(seen.values(), key=lambda x: x["total_amount"], reverse=True)[:10]


def _get_plan_analytics():
    """All plans (PPPoE + Hotspot) with transaction count and lifetime revenue."""
    from apps.billing.models.billing_models import Plan
    from apps.billing.models.hotspot_models import HotspotPlan
    from apps.billing.models.payment_models import Payment
    from django.db.models import Sum, Count, Q

    results = []

    # Billing plans (PPPoE etc.)
    # FIX: Match payments via invoice OR via customer's active service connection on this plan
    # This catches C2B payments where payment.customer is set and their service connection
    # links to the plan, even when no invoice is attached.
    for plan in Plan.objects.filter(is_active=True).values("id", "name", "plan_type", "base_price"):
        agg = (
            Payment.objects
            .filter(status__iexact="completed")
            .filter(
                Q(invoice__plan_id=plan["id"]) |
                Q(customer__services__plan_id=plan["id"])
            )
            .distinct()
            .aggregate(total=Sum("amount"), count=Count("id"))
        )
        results.append({
            "plan_type": "billing",
            "connection_type": plan.get("plan_type", "PPPOE"),
            "name": plan["name"],
            "base_price": _safe_float(plan["base_price"]),
            "total_revenue": _safe_float(agg["total"]),
            "total_transactions": agg["count"] or 0,
        })

    # Hotspot plans
    for plan in HotspotPlan.objects.filter(is_active=True).values("id", "name", "price"):
        agg = (
            Payment.objects
            .filter(status__iexact="completed", hotspot_session__plan_id=plan["id"])
            .aggregate(total=Sum("amount"), count=Count("id"))
        )
        results.append({
            "plan_type": "hotspot",
            "connection_type": "HOTSPOT",
            "name": plan["name"],
            "base_price": _safe_float(plan["price"]),
            "total_revenue": _safe_float(agg["total"]),
            "total_transactions": agg["count"] or 0,
        })

    return sorted(results, key=lambda x: x["total_revenue"], reverse=True)


class _RangeMixin:
    ALLOWED = {
        "reports": {"7d", "30d", "90d"},
        "churn": {"30d", "90d", "12m"},
        "customers": {"7d", "30d", "90d", "12m"},
        "revenue": {"7d", "30d", "90d", "12m"},
        "usage": {"24h", "7d", "30d"},
        "network_deep": {"7d", "30d", "90d"},
        "revenue_split": {"7d", "30d", "90d"},
        "router_revenue": {"7d", "30d", "90d"},
    }

    def _start(self, key, time_range):
        now = timezone.now()
        if time_range not in self.ALLOWED[key]:
            return None
        if time_range == "24h":
            return now - timedelta(hours=24)
        if time_range == "7d":
            return now - timedelta(days=7)
        if time_range == "30d":
            return now - timedelta(days=30)
        if time_range == "90d":
            return now - timedelta(days=90)
        if time_range == "12m":
            return now - relativedelta(months=12)
        return None

    def _cache_key(self, request, endpoint, time_range):
        # FIXED: Use connection.schema_name instead of user attributes
        # The old code used tenant_hint from user (which is None for tenant admin users)
        # causing all tenants to share the same cache key "global"
        schema = connection.schema_name
        return f"analytics:v2:{endpoint}:{schema}:{time_range}"


class AnalyticsReportsView(APIView, _RangeMixin):
    """
    GET /api/analytics/reports/?time_range=7d|30d|90d
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def _get_weekly_income(self, target_date, week_offset=0):
        """
        Get weekly income for a given week.
        week_offset: 0 = current week (Mon-Sun containing target_date)
                     -1 = previous week
        Returns list of 7 objects with day labels and amounts.
        """
        # Get Monday of the week containing target_date
        days_to_monday = target_date.weekday()  # Monday=0, Sunday=6
        week_start = target_date - timedelta(days=days_to_monday)
        # Apply week offset
        week_start = week_start + timedelta(weeks=week_offset)
        week_end = week_start + timedelta(days=7)

        # For the current week, cap the end at now so future weekdays always return 0.
        # Without this, a UTC payment made on "Sunday" at 11 PM EAT (= 8 PM UTC) can
        # appear inside the Mon–Sun window even though Sunday hasn't arrived locally yet.
        effective_end = week_end
        if week_offset == 0:
            week_end_dt = timezone.make_aware(
                datetime.combine(week_end, datetime.min.time()),
                timezone.get_current_timezone(),
            )
            effective_end = min(week_end_dt, timezone.now())

        payments = Payment.objects.filter(
            status__iexact="completed",
            payment_date__gte=timezone.make_aware(
                datetime.combine(week_start, datetime.min.time()),
                timezone.get_current_timezone(),
            ),
            payment_date__lt=effective_end,
        )

        # Aggregate by weekday
        # FIX: Convert UTC to local timezone before calling .weekday()
        weekday_map = {i: 0 for i in range(7)}
        for p in payments:
            local_dt = timezone.localtime(p.payment_date)
            weekday = local_dt.weekday()
            weekday_map[weekday] += _safe_float(p.amount)

        # Build result with day labels (Mon-Sun)
        day_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return [
            {"day": day_labels[i], "amount": round(weekday_map[i], 2)}
            for i in range(7)
        ]

    def _get_monthly_earnings(self, year, include_future=False):
        """
        Get monthly earnings for a given year.
        include_future: if True, include all 12 months even if future
                       if False, only include months up to current month
        Returns list of month objects with labels and amounts.
        """
        now = timezone.localtime(timezone.now())
        current_year = now.year
        current_month = now.month

        month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                       "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

        result = []
        for month in range(1, 13):
            # Skip future months if not allowed
            if not include_future and year == current_year and month > current_month:
                break

            month_start = datetime(year, month, 1, tzinfo=timezone.get_current_timezone())
            if month == 12:
                month_end = datetime(year + 1, 1, 1, tzinfo=timezone.get_current_timezone())
            else:
                month_end = datetime(year, month + 1, 1, tzinfo=timezone.get_current_timezone())

            # Query payments for this month
            total = _safe_float(
                Payment.objects.filter(
                    status__iexact="completed",
                    payment_date__gte=month_start,
                    payment_date__lt=month_end
                ).aggregate(v=Sum("amount"))["v"]
            )

            result.append({
                "month": month_labels[month - 1],
                "amount": round(total, 2)
            })

        return result

    def get(self, request):
        time_range = request.query_params.get("time_range", "30d")
        start = self._start("reports", time_range)
        if not start:
            return Response({"error": "Invalid time_range. Use 7d, 30d, 90d."}, status=400)

        ck = self._cache_key(request, "reports", time_range)
        cached = cache.get(ck)
        if cached:
            return Response(cached)

        # FIX: Convert to local time before computing day/week/month boundaries
        now = timezone.now()
        local_now = timezone.localtime(now)  # convert UTC -> Africa/Nairobi
        today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        
        # ============================================================
        # FIX: Use Monday-anchored calendar week instead of rolling 7 days
        # This matches weekly_income chart and fixes week-over-week % change
        # ============================================================
        days_to_monday = today_start.weekday()  # Monday=0 ... Sunday=6
        week_start = today_start - timedelta(days=days_to_monday)   # ✅ Monday 00:00 of current week
        prev_week_start = week_start - timedelta(days=7)            # ✅ Previous Monday 00:00
        
        month_start = today_start.replace(day=1)
        prev_month_start = (month_start - timedelta(days=1)).replace(day=1)

        # ONE aggregate query for key windows (payments)
        pay_base = Payment.objects.filter(status__iexact="completed")
        window = pay_base.aggregate(
            today_amount=Sum("amount", filter=Q(created_at__gte=today_start)),
            today_tx=Count("id", filter=Q(created_at__gte=today_start)),
            y_amount=Sum("amount", filter=Q(created_at__gte=yesterday_start, created_at__lt=today_start)),
            y_tx=Count("id", filter=Q(created_at__gte=yesterday_start, created_at__lt=today_start)),
            w_amount=Sum("amount", filter=Q(created_at__gte=week_start)),
            w_tx=Count("id", filter=Q(created_at__gte=week_start)),
            pw_amount=Sum("amount", filter=Q(created_at__gte=prev_week_start, created_at__lt=week_start)),
            pw_tx=Count("id", filter=Q(created_at__gte=prev_week_start, created_at__lt=week_start)),
            m_amount=Sum("amount", filter=Q(created_at__gte=month_start)),
            m_tx=Count("id", filter=Q(created_at__gte=month_start)),
            pm_amount=Sum("amount", filter=Q(created_at__gte=prev_month_start, created_at__lt=month_start)),
            pm_tx=Count("id", filter=Q(created_at__gte=prev_month_start, created_at__lt=month_start)),
        )

        today_amount = _safe_float(window["today_amount"])
        y_amount = _safe_float(window["y_amount"])
        w_amount = _safe_float(window["w_amount"])
        pw_amount = _safe_float(window["pw_amount"])
        m_amount = _safe_float(window["m_amount"])
        pm_amount = _safe_float(window["pm_amount"])

        # hourly revenue (single grouped query)
        hourly = (
            pay_base.filter(created_at__gte=today_start)
            .annotate(h=TruncHour("created_at"))
            .values("h")
            .annotate(revenue=Sum("amount"))
            .order_by("h")
        )
        hourly_rows = []
        for r in hourly:
            hour = r["h"].hour
            rev = _safe_float(r["revenue"])
            item = {"hour": f"{hour:02d}:00", "peak_hours": 0, "business_hours": 0, "off_hours": 0}
            if 18 <= hour <= 23:
                item["peak_hours"] = rev
            elif 8 <= hour <= 17:
                item["business_hours"] = rev
            else:
                item["off_hours"] = rev
            hourly_rows.append(item)

        # registration trend (single grouped query)
        regs = (
            Customer.objects.filter(created_at__gte=start)
            .annotate(d=TruncDay("created_at"))
            .values("d")
            .annotate(count=Count("id"))
            .order_by("d")
        )
        reg_rows = [{"date": x["d"].date().isoformat(), "count": x["count"]} for x in regs]

        # FIX A: network flow using correct field names (period_start, upload_bytes, download_bytes)
        flow = (
            DataUsage.objects.filter(period_start__gte=start)
            .annotate(d=TruncDay("period_start"))
            .values("d")
            .annotate(upload=Sum("upload_bytes"), download=Sum("download_bytes"))
            .order_by("d")
        )
        flow_rows = [{
            "date": x["d"].date().isoformat(),
            "upload": round((x["upload"] or 0) / (1024 ** 3), 2),      # Convert to GB
            "download": round((x["download"] or 0) / (1024 ** 3), 2),  # Convert to GB
        } for x in flow]

        # ============================================================
        # NEW: Weekly Income Data (Current Week)
        # ============================================================
        today_date = local_now.date()
        weekly_income = self._get_weekly_income(today_date, week_offset=0)

        # ============================================================
        # NEW: Last Week Income Data
        # ============================================================
        last_week_income = self._get_weekly_income(today_date, week_offset=-1)

        # ============================================================
        # NEW: Monthly Earnings (Current Year, up to current month)
        # ============================================================
        current_year = local_now.year
        monthly_earnings = self._get_monthly_earnings(current_year, include_future=False)

        # ============================================================
        # NEW: Last Year Earnings (Full calendar year)
        # ============================================================
        last_year = current_year - 1
        last_year_earnings = self._get_monthly_earnings(last_year, include_future=True)

        payload = {
            "overview": {
                "today_revenue": today_amount,
                "today_change": _pct(today_amount, y_amount),
                "week_revenue": w_amount,
                "week_change": _pct(w_amount, pw_amount),
                "month_revenue": m_amount,
                "month_change": _pct(m_amount, pm_amount),
                "total_transactions_today": int(window["today_tx"] or 0),
                "hourly_revenue": hourly_rows,
                "user_registrations": reg_rows,
                "network_data_flow": flow_rows,
                # New fields for weekly income charts
                "weekly_income": weekly_income,
                "last_week_income": last_week_income,
                # New fields for monthly earnings charts
                "monthly_earnings": monthly_earnings,
                "last_year_earnings": last_year_earnings,
            },
            "financial": {
                "income_comparison": {
                    "today": {"amount": today_amount, "transactions": int(window["today_tx"] or 0)},
                    "yesterday": {"amount": y_amount, "transactions": int(window["y_tx"] or 0)},
                    "this_week": {"amount": w_amount, "transactions": int(window["w_tx"] or 0)},
                    "last_week": {"amount": pw_amount, "transactions": int(window["pw_tx"] or 0)},
                },
                "monthly_performance": {
                    "this_month": {"amount": m_amount, "transactions": int(window["m_tx"] or 0)},
                    "last_month": {"amount": pm_amount, "transactions": int(window["pm_tx"] or 0)},
                    "growth_rate": _pct(m_amount, pm_amount),
                },
                "hourly_revenue": hourly_rows,
            },
            "users": {
                "registration_trends": reg_rows,
                "summary": {
                    "total_registrations": sum(r["count"] for r in reg_rows),
                    "avg_per_day": round(sum(r["count"] for r in reg_rows) / max(len(reg_rows), 1), 2),
                    "peak_day": max(reg_rows, key=lambda x: x["count"])["date"] if reg_rows else None,
                },
            },
            "network": {
                "daily_usage": flow_rows,
                "usage_summary": {
                    "total_upload": round(sum(x["upload"] for x in flow_rows), 2),
                    "total_download": round(sum(x["download"] for x in flow_rows), 2),
                    "total_usage": round(sum(x["upload"] + x["download"] for x in flow_rows), 2),
                },
                "performance": {
                    "peak_usage_day": max(flow_rows, key=lambda x: x["upload"] + x["download"])["date"] if flow_rows else None,
                    "avg_daily_usage": f"{round(sum(x['upload'] + x['download'] for x in flow_rows) / max(len(flow_rows), 1), 2)} GB",
                    "download_upload_ratio": (
                        f"{round((sum(x['download'] for x in flow_rows) / max(sum(x['upload'] for x in flow_rows), 1)), 2)}:1"
                        if flow_rows else "0:1"
                    ),
                },
            },
        }

        # Add the two new fields right before cache.set
        payload["top_customers"] = _get_top_impactful_customers()
        payload["plan_analytics"] = _get_plan_analytics()

        cache.set(ck, payload, CACHE_TTL)
        return Response(payload)


class AnalyticsChurnView(APIView, _RangeMixin):
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        tr = request.query_params.get("time_range", "90d")
        start = self._start("churn", tr)
        if not start:
            return Response({"error": "Invalid time_range. Use 30d, 90d, 12m."}, status=400)

        ck = self._cache_key(request, "churn", tr)
        if (cached := cache.get(ck)):
            return Response(cached)

        now = timezone.localtime(timezone.now())
        total = Customer.objects.count()

        churn_qs = Customer.objects.filter(status__iexact="terminated", updated_at__gte=start)
        churned = churn_qs.count()
        churn_rate = round((churned / total) * 100, 2) if total else 0

        revenue_lost = _safe_float(
            Payment.objects.filter(status__iexact="completed", customer__in=churn_qs).aggregate(v=Sum("amount"))["v"]
        )

        # At-risk = active customers with stale payments
        active_customers = Customer.objects.filter(status__iexact="active").annotate(
            last_payment=models.Max("payments__payment_date", filter=Q(payments__status__iexact="completed"))
        )

        at_risk_rows = []
        for c in active_customers:
            if not c.last_payment:
                days_inactive = 999
            else:
                days_inactive = (now.date() - c.last_payment.date()).days

            if days_inactive < 14:
                continue

            # simple risk score
            risk = min(100, max(0, int(days_inactive * 1.8)))
            at_risk_rows.append({
                "name": c.full_name or c.customer_code,
                "plan": ServiceConnection.objects.filter(customer=c, status__iexact="active").values_list("plan__name", flat=True).first() or "Unknown",
                "daysInactive": days_inactive,
                "lastPayment": c.last_payment.date().isoformat() if c.last_payment else None,
                "riskScore": risk,
            })

        at_risk_rows.sort(key=lambda x: x["riskScore"], reverse=True)
        at_risk_rows = at_risk_rows[:100]

        # Try to get churn reasons from model (if exists)
        try:
            from apps.analytics.models import CustomerChurnEvent
            reason_qs = (
                CustomerChurnEvent.objects.filter(created_at__gte=start)
                .values("reason")
                .annotate(count=Count("id"))
                .order_by("-count")
            )
            reason_total = sum(x["count"] for x in reason_qs) or 1
            reasons = [{
                "reason": x["reason"],
                "count": x["count"],
                "percentage": round((x["count"] / reason_total) * 100, 2),
            } for x in reason_qs]
        except (ImportError, models.Model.DoesNotExist):
            reasons = []

        # monthly trend
        trend_qs = (
            churn_qs.annotate(m=TruncMonth("updated_at"))
            .values("m")
            .annotate(churned=Count("id"))
            .order_by("m")
        )
        trend = []
        for t in trend_qs:
            month_start = t["m"]
            month_end = month_start + relativedelta(months=1)
            month_total = Customer.objects.filter(created_at__lt=month_end).count() or 1
            month_churn_rate = round((t["churned"] / month_total) * 100, 2)
            month_rev = _safe_float(
                Payment.objects.filter(
                    status__iexact="completed",
                    customer__in=Customer.objects.filter(status__iexact="terminated", updated_at__gte=month_start, updated_at__lt=month_end)
                ).aggregate(v=Sum("amount"))["v"]
            )
            trend.append({
                "month": month_start.strftime("%b %Y"),
                "churned": t["churned"],
                "rate": month_churn_rate,
                "revenue": month_rev,
            })

        payload = {
            "churnStats": {
                "churnRate": churn_rate,
                "churnedThisMonth": churned,
                "atRisk": len(at_risk_rows),
                "revenueLost": revenue_lost,
                "avgLifetimeBeforeChurn": 0,  # optionally compute like customers avgLifetime on churn subset
                "winbackRate": 0,             # implement from re-activated churn events if tracked
            },
            "churnReasons": reasons,
            "atRiskCustomers": at_risk_rows,
            "churnTrend": trend,
        }
        cache.set(ck, payload, CACHE_TTL)
        return Response(payload)


class AnalyticsCustomersView(APIView, _RangeMixin):
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        tr = request.query_params.get("time_range", "30d")
        start = self._start("customers", tr)
        if not start:
            return Response({"error": "Invalid time_range. Use 7d, 30d, 90d, 12m."}, status=400)

        ck = self._cache_key(request, "customers", tr)
        if (cached := cache.get(ck)):
            return Response(cached)

        now = timezone.localtime(timezone.now())
        stats = Customer.objects.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(status__iexact="active")),
            new=Count("id", filter=Q(created_at__gte=start)),
            churned=Count("id", filter=Q(status__iexact="terminated", updated_at__gte=start)),
        )
        total = stats["total"] or 0
        active = stats["active"] or 0
        new = stats["new"] or 0
        churned = stats["churned"] or 0

        # avg lifetime from terminated customers (days)
        life_qs = Customer.objects.filter(status__iexact="terminated").exclude(updated_at__isnull=True)
        avg_lifetime_days = life_qs.annotate(
            lifetime=ExpressionWrapper(F("updated_at") - F("created_at"), output_field=models.DurationField())
        ).aggregate(v=Avg("lifetime"))["v"]
        avg_lifetime = round(avg_lifetime_days.days if avg_lifetime_days else 0, 2)

        # LTV = avg revenue per customer over selected range
        pay_sum = _safe_float(Payment.objects.filter(status__iexact="completed", payment_date__gte=start).aggregate(v=Sum("amount"))["v"])
        ltv = round(pay_sum / max(total, 1), 2)

        # by plan
        plan_raw = (
            ServiceConnection.objects.filter(status__iexact="active")
            .values("plan__name")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        plan_total = sum(x["count"] for x in plan_raw) or 1
        by_plan = [{
            "plan": x["plan__name"] or "Unknown",
            "count": x["count"],
            "percentage": round((x["count"] / plan_total) * 100, 2),
            "growth": 0,
        } for x in plan_raw]

        # location (county/sub-county fallback)
        loc_raw = (
            Customer.objects.values("addresses__sub_county", "addresses__county")
            .annotate(count=Count("id"))
            .order_by("-count")[:30]
        )
        loc_total = sum(x["count"] for x in loc_raw) or 1
        by_loc = [{
            "location": x["addresses__sub_county"] or x["addresses__county"] or "Unknown",
            "count": x["count"],
            "percentage": round((x["count"] / loc_total) * 100, 2),
        } for x in loc_raw]

        # cohorts (last 12 months)
        cohorts = []
        for i in range(12):
            cohort_start = (now - relativedelta(months=i)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            cohort_end = (cohort_start + relativedelta(months=1))
            acquired_qs = Customer.objects.filter(created_at__gte=cohort_start, created_at__lt=cohort_end)
            acquired = acquired_qs.count()
            if acquired == 0:
                continue

            ids = list(acquired_qs.values_list("id", flat=True))

            def retained(month_offset):
                s = cohort_end + relativedelta(months=month_offset - 1)
                e = s + relativedelta(months=1)
                c = Payment.objects.filter(
                    status__iexact="completed",
                    customer_id__in=ids,
                    payment_date__gte=s,
                    payment_date__lt=e
                ).values("customer_id").distinct().count()
                return round((c / acquired) * 100, 2) if acquired else 0

            cohorts.append({
                "cohort": cohort_start.strftime("%b %Y"),
                "acquired": acquired,
                "month1": retained(1),
                "month2": retained(2),
                "month3": retained(3),
            })

        payload = {
            "customerStats": {
                "total": total,
                "active": active,
                "new": new,
                "churned": churned,
                "growthRate": round((new / max(total - new, 1)) * 100, 2) if total else 0,
                "retentionRate": round((active / total) * 100, 2) if total else 0,
                "avgLifetime": avg_lifetime,
                "ltv": ltv,
            },
            "customersByPlan": by_plan,
            "customersByLocation": by_loc,
            "cohortData": cohorts,
        }
        cache.set(ck, payload, CACHE_TTL)
        return Response(payload)


class AnalyticsRevenueView(APIView, _RangeMixin):
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        tr = request.query_params.get("time_range", "30d")
        start = self._start("revenue", tr)
        if not start:
            return Response({"error": "Invalid time_range. Use 7d, 30d, 90d, 12m."}, status=400)

        ck = self._cache_key(request, "revenue", tr)
        if (cached := cache.get(ck)):
            return Response(cached)

        now = timezone.localtime(timezone.now())
        prev_start = start - (now - start)

        pay_cur = Payment.objects.filter(status__iexact="completed", payment_date__gte=start)
        pay_prev = Payment.objects.filter(status__iexact="completed", payment_date__gte=prev_start, payment_date__lt=start)

        cur_total = _safe_float(pay_cur.aggregate(v=Sum("amount"))["v"])
        prev_total = _safe_float(pay_prev.aggregate(v=Sum("amount"))["v"])

        active_customers = Customer.objects.filter(status__iexact="active").count()
        arpu = round(cur_total / active_customers, 2) if active_customers else 0

        prev_active = Customer.objects.filter(status__iexact="active", created_at__lt=start).count()
        prev_arpu = round(prev_total / prev_active, 2) if prev_active else 0

        # monthly trend + growth
        month_rows = (
            pay_cur.annotate(m=TruncMonth("payment_date"))
            .values("m")
            .annotate(revenue=Coalesce(Sum("amount"), 0), transactions=Count("id"))
            .order_by("m")
        )
        monthly = []
        last_rev = None
        for r in month_rows:
            rev = _safe_float(r["revenue"])
            monthly.append({
                "month": r["m"].strftime("%b %Y"),
                "revenue": rev,
                "growth": _pct(rev, last_rev) if last_rev is not None else 0,
                "transactions": int(r["transactions"] or 0),
            })
            last_rev = rev

        # payment methods normalized
        pm_rows = pay_cur.values("payment_method__name").annotate(amount=Sum("amount"), count=Count("id"))
        buckets = {}
        for row in pm_rows:
            k = _norm_method(row["payment_method__name"])
            buckets.setdefault(k, {"amount": 0, "count": 0})
            buckets[k]["amount"] += _safe_float(row["amount"])
            buckets[k]["count"] += int(row["count"] or 0)

        method_total = sum(v["amount"] for v in buckets.values()) or 1
        methods = [{
            "method": k,
            "amount": v["amount"],
            "count": v["count"],
            "percentage": round((v["amount"] / method_total) * 100, 2),
        } for k, v in buckets.items()]

        # MRR = this calendar month completed payments
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_month_start = (month_start - timedelta(days=1)).replace(day=1)

        mrr = _safe_float(Payment.objects.filter(status__iexact="completed", payment_date__gte=month_start).aggregate(v=Sum("amount"))["v"])
        prev_mrr = _safe_float(Payment.objects.filter(status__iexact="completed", payment_date__gte=prev_month_start, payment_date__lt=month_start).aggregate(v=Sum("amount"))["v"])

        # by plan
        plan_subs = {
            x["plan__name"]: x["subs"]
            for x in ServiceConnection.objects.filter(status__iexact="active")
            .values("plan__name").annotate(subs=Count("id"))
        }
        plan_rev_qs = pay_cur.values("invoice__plan__name").annotate(revenue=Sum("amount")).order_by("-revenue")
        rev_total = sum(_safe_float(x["revenue"]) for x in plan_rev_qs) or 1
        by_plan = [{
            "plan": x["invoice__plan__name"] or "Unknown",
            "revenue": _safe_float(x["revenue"]),
            "subscribers": int(plan_subs.get(x["invoice__plan__name"], 0)),
            "percentage": round((_safe_float(x["revenue"]) / rev_total) * 100, 2),
        } for x in plan_rev_qs]

        payload = {
            "revenueStats": {
                "total": cur_total,
                "growth": _pct(cur_total, prev_total),
                "arpu": arpu,
                "arpuGrowth": _pct(arpu, prev_arpu),
                "mrr": mrr,
                "mrrGrowth": _pct(mrr, prev_mrr),
                "arr": mrr * 12,
                "arrGrowth": _pct(mrr * 12, prev_mrr * 12),
            },
            "monthlyRevenue": monthly,
            "revenueByPlan": by_plan,
            "paymentMethods": methods,
        }
        cache.set(ck, payload, CACHE_TTL)
        return Response(payload)


class AnalyticsUsageView(APIView, _RangeMixin):
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        tr = request.query_params.get("time_range", "7d")
        start = self._start("usage", tr)
        if not start:
            return Response({"error": "Invalid time_range. Use 24h, 7d, 30d."}, status=400)

        ck = self._cache_key(request, "usage", tr)
        if (cached := cache.get(ck)):
            return Response(cached)

        # FIX B: Use correct field names (period_start, upload_bytes, download_bytes)
        usage = DataUsage.objects.filter(period_start__gte=start)

        totals = usage.aggregate(
            down=Coalesce(Sum("download_bytes"), 0),
            up=Coalesce(Sum("upload_bytes"), 0),
            avg_daily_bytes=Coalesce(Avg(F("download_bytes") + F("upload_bytes")), 0),
        )
        total_down = totals["down"]
        total_up = totals["up"]

        # Hourly usage from period_start
        hour_rows = (
            usage.annotate(h=TruncHour("period_start"))
            .values("h")
            .annotate(
                download_bytes=Coalesce(Sum("download_bytes"), 0),
                upload_bytes=Coalesce(Sum("upload_bytes"), 0),
                users=Count("customer", distinct=True),
            )
            .order_by("h")
        )

        # Convert bytes-per-hour bucket to approximate Mbps
        hourly = []
        for r in hour_rows:
            down_mbps = round((r["download_bytes"] * 8) / 3600 / (1024 * 1024), 3)
            up_mbps = round((r["upload_bytes"] * 8) / 3600 / (1024 * 1024), 3)
            hourly.append({
                "hour": r["h"].strftime("%H:00"),
                "download": down_mbps,
                "upload": up_mbps,
                "users": int(r["users"] or 0),
            })

        peak_hour = max(hourly, key=lambda x: x["download"] + x["upload"])["hour"] if hourly else None

        # Top users
        top_users_qs = (
            usage.values("customer_id", "customer__full_name", "customer__customer_code")
            .annotate(
                download_bytes=Coalesce(Sum("download_bytes"), 0),
                upload_bytes=Coalesce(Sum("upload_bytes"), 0),
                sessions=Count("id"),
            )
            .order_by("-download_bytes", "-upload_bytes")[:10]
        )

        # Active plan per customer
        active_plan_map = dict(
            ServiceConnection.objects.filter(status__iexact="active")
            .values_list("customer_id", "plan__name")
        )

        top_users = [{
            "name": x["customer__full_name"] or x["customer__customer_code"] or f"Customer {x['customer_id']}",
            "plan": active_plan_map.get(x["customer_id"], "Unknown"),
            "download": _bytes_to_gb(x["download_bytes"]),
            "upload": _bytes_to_gb(x["upload_bytes"]),
            "sessions": int(x["sessions"] or 0),
        } for x in top_users_qs]

        # Usage by plan
        by_plan_qs = (
            usage.values("customer_id")
            .annotate(
                d=Coalesce(Sum("download_bytes"), 0),
                u=Coalesce(Sum("upload_bytes"), 0),
            )
        )
        plan_bucket = {}
        total_bytes = 0
        for row in by_plan_qs:
            plan = active_plan_map.get(row["customer_id"], "Unknown")
            b = int(row["d"] or 0) + int(row["u"] or 0)
            total_bytes += b
            plan_bucket.setdefault(plan, {"bytes": 0, "count": 0, "down": 0, "up": 0})
            plan_bucket[plan]["bytes"] += b
            plan_bucket[plan]["down"] += int(row["d"] or 0)
            plan_bucket[plan]["up"] += int(row["u"] or 0)
            plan_bucket[plan]["count"] += 1

        usage_by_plan = []
        for plan, data in plan_bucket.items():
            c = max(data["count"], 1)
            usage_by_plan.append({
                "plan": plan,
                "avgDownload": _bytes_to_gb(data["down"] / c),
                "avgUpload": _bytes_to_gb(data["up"] / c),
                "percentage": round((data["bytes"] / max(total_bytes, 1)) * 100, 2),
            })
        usage_by_plan.sort(key=lambda x: x["percentage"], reverse=True)

        payload = {
            "usageStats": {
                "totalDownload": _bytes_to_tb(total_down),   # Convert to TB
                "totalUpload": _bytes_to_tb(total_up),       # Convert to TB
                "peakHour": peak_hour,
                "avgSessionDuration": 0,  # add from RadiusAccounting if available
                "heavyUsers": len([u for u in top_users if u["download"] > 100]),  # >100GB sample threshold
                "avgDailyUsage": _bytes_to_gb(totals["avg_daily_bytes"]),  # Convert to GB
            },
            "hourlyUsage": hourly,
            "usageByPlan": usage_by_plan,
            "topUsers": top_users,
        }
        cache.set(ck, payload, CACHE_TTL)
        return Response(payload)


# ==========================================
# ROUTER DAILY REVENUE VIEW
# ==========================================

class RouterDailyRevenueView(APIView, _RangeMixin):
    """
    GET /api/analytics/router-revenue/?time_range=7d|30d|90d&router_id=<id>
    Returns daily hotspot revenue for a specific router.
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        time_range = request.query_params.get("time_range", "30d")
        router_id = request.query_params.get("router_id")
        start = self._start("reports", time_range)
        if not start:
            return Response({"error": "Invalid time_range. Use 7d, 30d, 90d."}, status=400)

        from apps.billing.models.payment_models import Payment
        from apps.network.models.router_models import Router
        from django.db.models import Sum, Count
        from django.db.models.functions import TruncDay

        # Get all routers for dropdown
        routers = Router.objects.filter(is_active=True).values("id", "name")
        routers_list = [{"id": r["id"], "name": r["name"]} for r in routers]

        if not router_id and routers_list:
            router_id = routers_list[0]["id"]

        if not router_id:
            return Response({"routers": routers_list, "daily_revenue": [], "summary": {}})

        # Daily hotspot revenue for selected router
        qs = Payment.objects.filter(
            status__iexact="completed",
            payment_date__gte=start,
            hotspot_sessions__router_id=router_id,
        ).annotate(
            day=TruncDay("payment_date")
        ).values("day").annotate(
            revenue=Sum("amount"),
            transactions=Count("id"),
        ).order_by("day")

        # Build full date range (fill gaps with 0)
        from datetime import timedelta
        from django.utils import timezone as tz

        day_map = {}
        for r in qs:
            day_map[r["day"].date()] = {
                "revenue": _safe_float(r["revenue"]),
                "transactions": r["transactions"],
            }

        result = []
        cursor = start.date()
        end = tz.now().date()
        while cursor <= end:
            vals = day_map.get(cursor, {"revenue": 0, "transactions": 0})
            result.append({
                "date": cursor.strftime("%b %d"),
                "revenue": vals["revenue"],
                "transactions": vals["transactions"],
            })
            cursor += timedelta(days=1)

        total = sum(r["revenue"] for r in result)
        peak = max(result, key=lambda x: x["revenue"]) if result else None

        return Response({
            "routers": routers_list,
            "selected_router_id": router_id,
            "daily_revenue": result,
            "summary": {
                "total_revenue": round(total, 2),
                "avg_daily": round(total / max(len(result), 1), 2),
                "peak_day": peak["date"] if peak else "-",
                "peak_amount": peak["revenue"] if peak else 0,
            },
        })


# ================================================================
# PPPoE vs Hotspot Daily Revenue Split View
# ================================================================

class DailyRevenueSplitView(APIView, _RangeMixin):
    """
    GET /api/analytics/revenue-split/?time_range=7d|30d|90d
    Daily revenue split between PPPoE (Fiber/DSL) and Hotspot.
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        time_range = request.query_params.get("time_range", "30d")
        start = self._start("reports", time_range)
        if not start:
            return Response({"error": "Invalid time_range. Use 7d, 30d, 90d."}, status=400)

        ck = self._cache_key(request, "revenue_split", time_range)
        cached = cache.get(ck)
        if cached:
            return Response(cached)

        qs = Payment.objects.filter(status__iexact="completed", payment_date__gte=start)

        # FIX 3: Classify hotspot via FK OR account_reference/notes signals
        # This handles historical data where hotspot_session is NULL but the payment
        # is clearly a hotspot payment based on account reference pattern or notes.
        HOTSPOT_Q = (
            Q(hotspot_session__isnull=False) |
            Q(mpesa_transaction__account_reference__istartswith="HS_") |
            Q(notes__icontains="hotspot")
        )

        rows = (
            qs.annotate(day=TruncDay("payment_date"))
            .values("day")
            .annotate(
                hotspot_revenue=Sum("amount", filter=HOTSPOT_Q),
                pppoe_revenue=Sum("amount", filter=~HOTSPOT_Q),
            )
            .order_by("day")
        )

        day_map = {
            r["day"].date(): {
                "hotspot": _safe_float(r["hotspot_revenue"]),
                "pppoe": _safe_float(r["pppoe_revenue"]),
            }
            for r in rows
        }

        result = []
        cursor = start.date()
        end = timezone.now().date()
        while cursor <= end:
            vals = day_map.get(cursor, {"hotspot": 0, "pppoe": 0})
            result.append({
                "date": cursor.strftime("%b %d"),
                "hotspot_revenue": vals["hotspot"],
                "pppoe_revenue": vals["pppoe"],
                "total": round(vals["hotspot"] + vals["pppoe"], 2),
            })
            cursor += timedelta(days=1)

        payload = {
            "daily_revenue": result,
            "summary": {
                "total_hotspot": round(sum(r["hotspot_revenue"] for r in result), 2),
                "total_pppoe": round(sum(r["pppoe_revenue"] for r in result), 2),
            },
        }
        cache.set(ck, payload, CACHE_TTL)
        return Response(payload)


# ================================================================
# NEW: AnalyticsNetworkDeepView - Enhanced Network Analytics
# ================================================================

class AnalyticsNetworkDeepView(APIView, _RangeMixin):
    """
    GET /api/analytics/network-deep/?time_range=7d|30d|90d
    Rich network analytics for the enhanced Network tab.
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        tr = request.query_params.get("time_range", "7d")
        start = self._start("network_deep", tr)
        if not start:
            return Response({"error": "Invalid time_range."}, status=400)

        ck = self._cache_key(request, "network_deep", tr)
        if (cached := cache.get(ck)):
            return Response(cached)

        payload = {
            "session_heatmap": self._session_heatmap(start),
            "router_health": self._router_health(start),
            "auth_trend": self._auth_trend(start),
            "top_bandwidth_users": self._top_bandwidth(start),
            "protocol_distribution": self._protocol_dist(start),
            "hourly_sessions": self._hourly_sessions(start),
            "termination_causes": self._termination_causes(start),
            "new_vs_returning": self._new_vs_returning(start),
        }
        cache.set(ck, payload, CACHE_TTL)
        return Response(payload)

    def _session_heatmap(self, start):
        """
        7x24 heatmap: sessions per (weekday, hour).
        Returns list of {day: 0-6, hour: 0-23, value: count}
        """
        from apps.radius.models import RadAcct
        from django.db.models.functions import ExtractHour, ExtractWeekDay

        qs = (
            RadAcct.objects
            .filter(acctstarttime__gte=start)
            .annotate(
                hour=ExtractHour("acctstarttime"),
                weekday=ExtractWeekDay("acctstarttime"),
            )
            .values("hour", "weekday")
            .annotate(count=Count("radacctid"))
        )

        # weekday: Django returns 1=Sunday...7=Saturday, remap to 0=Mon..6=Sun
        REMAP = {1: 6, 2: 0, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5}
        result = []
        for r in qs:
            result.append({
                "day": REMAP.get(r["weekday"], 0),
                "hour": r["hour"],
                "value": r["count"],
            })
        return result

    def _router_health(self, start):
        """
        Per-router: session count, avg session time, total GB, auth failures.
        """
        from apps.radius.models import RadAcct, RadPostAuth
        from apps.network.models.router_models import Router

        routers = Router.objects.filter(is_active=True).values("id", "name", "status", "vpn_ip_address", "ip_address")

        result = []
        for r in routers:
            ip = r["vpn_ip_address"] or r["ip_address"]
            if not ip:
                continue
            acct = RadAcct.objects.filter(
                nasipaddress=ip,
                acctstarttime__gte=start,
            ).aggregate(
                sessions=Count("radacctid"),
                avg_time=Avg("acctsessiontime"),
                total_in=Sum("acctinputoctets"),
                total_out=Sum("acctoutputoctets"),
            )
            auth_fails = RadPostAuth.objects.filter(
                nasipaddress=ip,
                authdate__gte=start,
                reply="Access-Reject",
            ).count()

            total_gb = _bytes_to_gb((acct["total_in"] or 0) + (acct["total_out"] or 0))
            avg_minutes = round((acct["avg_time"] or 0) / 60, 1)
            result.append({
                "id": r["id"],
                "name": r["name"],
                "status": r["status"],
                "sessions": acct["sessions"] or 0,
                "avg_session_minutes": avg_minutes,
                "total_gb": total_gb,
                "auth_failures": auth_fails,
                "health_score": min(100, max(0, 100 - (auth_fails * 2))),
            })

        return sorted(result, key=lambda x: x["sessions"], reverse=True)

    def _auth_trend(self, start):
        """Daily auth success vs failure counts."""
        from apps.radius.models import RadPostAuth

        qs = (
            RadPostAuth.objects
            .filter(authdate__gte=start)
            .annotate(day=TruncDay("authdate"))
            .values("day", "reply")
            .annotate(count=Count("id"))
            .order_by("day")
        )

        day_map = {}
        for r in qs:
            d = r["day"].date().isoformat()
            if d not in day_map:
                day_map[d] = {"date": d, "success": 0, "failure": 0}
            if r["reply"] == "Access-Accept":
                day_map[d]["success"] += r["count"]
            else:
                day_map[d]["failure"] += r["count"]

        return sorted(day_map.values(), key=lambda x: x["date"])

    def _top_bandwidth(self, start):
        """Top 15 usernames by total bytes (in + out) in the period."""
        from apps.radius.models import RadAcct
        from django.db.models import BigIntegerField, ExpressionWrapper as EW, F

        qs2 = (
            RadAcct.objects
            .filter(acctstarttime__gte=start)
            .values("username")
            .annotate(
                bytes_in=Coalesce(Sum("acctinputoctets"), 0),
                bytes_out=Coalesce(Sum("acctoutputoctets"), 0),
            )
            .annotate(
                total=EW(F("bytes_in") + F("bytes_out"), output_field=BigIntegerField())
            )
            .order_by("-total")[:15]
        )

        return [{
            "username": r["username"],
            "download_gb": _bytes_to_gb(r["bytes_out"]),
            "upload_gb": _bytes_to_gb(r["bytes_in"]),
            "total_gb": _bytes_to_gb(r["total"]),
        } for r in qs2]

    def _protocol_dist(self, start):
        """PPPoE vs Hotspot session counts and data volume."""
        from apps.radius.models import RadAcct

        pppoe = RadAcct.objects.filter(
            acctstarttime__gte=start,
            framedprotocol="PPP",
        ).aggregate(sessions=Count("radacctid"), bytes_in=Sum("acctinputoctets"), bytes_out=Sum("acctoutputoctets"))

        hotspot = RadAcct.objects.filter(
            acctstarttime__gte=start,
        ).exclude(framedprotocol="PPP").aggregate(
            sessions=Count("radacctid"), bytes_in=Sum("acctinputoctets"), bytes_out=Sum("acctoutputoctets")
        )

        return {
            "pppoe": {
                "sessions": pppoe["sessions"] or 0,
                "total_gb": _bytes_to_gb((pppoe["bytes_in"] or 0) + (pppoe["bytes_out"] or 0)),
            },
            "hotspot": {
                "sessions": hotspot["sessions"] or 0,
                "total_gb": _bytes_to_gb((hotspot["bytes_in"] or 0) + (hotspot["bytes_out"] or 0)),
            },
        }

    def _hourly_sessions(self, start):
        """Average concurrent sessions per hour-of-day (aggregated across all days)."""
        from apps.radius.models import RadAcct
        from django.db.models.functions import ExtractHour

        qs = (
            RadAcct.objects
            .filter(acctstarttime__gte=start)
            .annotate(hour=ExtractHour("acctstarttime"))
            .values("hour")
            .annotate(session_count=Count("radacctid"))
            .order_by("hour")
        )
        hour_map = {r["hour"]: r["session_count"] for r in qs}
        return [{"hour": f"{h:02d}:00", "sessions": hour_map.get(h, 0)} for h in range(24)]

    def _termination_causes(self, start):
        """Top disconnect reasons."""
        from apps.radius.models import RadAcct

        qs = (
            RadAcct.objects
            .filter(acctstarttime__gte=start, acctstoptime__isnull=False)
            .exclude(acctterminatecause="")
            .exclude(acctterminatecause__isnull=True)
            .values("acctterminatecause")
            .annotate(count=Count("radacctid"))
            .order_by("-count")[:10]
        )
        total = sum(r["count"] for r in qs) or 1
        return [{
            "cause": r["acctterminatecause"],
            "count": r["count"],
            "percentage": round((r["count"] / total) * 100, 1),
        } for r in qs]

    def _new_vs_returning(self, start):
        """
        Daily: new unique usernames seen for first time vs returning ones.
        """
        from apps.radius.models import RadAcct
        from collections import defaultdict

        all_before = set(
            RadAcct.objects
            .filter(acctstarttime__lt=start)
            .values_list("username", flat=True)
            .distinct()
        )

        day_qs = (
            RadAcct.objects
            .filter(acctstarttime__gte=start)
            .annotate(day=TruncDay("acctstarttime"))
            .values("day", "username")
            .distinct()
            .order_by("day")
        )

        day_buckets = defaultdict(set)
        for r in day_qs:
            day_buckets[r["day"].date().isoformat()].add(r["username"])

        seen_so_far = set(all_before)
        result = []
        for day in sorted(day_buckets):
            users = day_buckets[day]
            new_users = users - seen_so_far
            returning = users & seen_so_far
            seen_so_far |= users
            result.append({
                "date": day,
                "new": len(new_users),
                "returning": len(returning),
            })
        return result