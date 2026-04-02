# apps/analytics/frontend_contract_views.py
from datetime import timedelta
from dateutil.relativedelta import relativedelta

from django.core.cache import cache
from django.db.models import Sum, Count, Avg, Q, F
from django.db.models.functions import TruncDay, TruncHour, TruncMonth
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


def _pct(cur, prev):
    if not prev:
        return 0.0
    return round(((cur - prev) / prev) * 100, 2)


def _safe_float(v):
    return float(v or 0)


class _RangeMixin:
    ALLOWED = {
        "reports": {"7d", "30d", "90d"},
        "churn": {"30d", "90d", "12m"},
        "customers": {"7d", "30d", "90d", "12m"},
        "revenue": {"7d", "30d", "90d", "12m"},
        "usage": {"24h", "7d", "30d"},
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
        tenant_hint = getattr(request.user, "tenant_id", None) or getattr(request.user, "company_id", None) or "global"
        return f"analytics:v2:{endpoint}:{tenant_hint}:{time_range}"


class AnalyticsReportsView(APIView, _RangeMixin):
    """
    GET /api/analytics/reports/?time_range=7d|30d|90d
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        time_range = request.query_params.get("time_range", "30d")
        start = self._start("reports", time_range)
        if not start:
            return Response({"error": "Invalid time_range. Use 7d, 30d, 90d."}, status=400)

        ck = self._cache_key(request, "reports", time_range)
        cached = cache.get(ck)
        if cached:
            return Response(cached)

        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        week_start = today_start - timedelta(days=7)
        prev_week_start = week_start - timedelta(days=7)
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

        # network flow (single grouped query)
        flow = (
            DataUsage.objects.filter(timestamp__gte=start)
            .annotate(d=TruncDay("timestamp"))
            .values("d")
            .annotate(upload=Sum("upload_mb"), download=Sum("download_mb"))
            .order_by("d")
        )
        flow_rows = [{
            "date": x["d"].date().isoformat(),
            "upload": _safe_float(x["upload"]),
            "download": _safe_float(x["download"]),
        } for x in flow]

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
                    "avg_daily_usage": f"{round(sum(x['upload'] + x['download'] for x in flow_rows) / max(len(flow_rows), 1), 2)} MB",
                    "download_upload_ratio": (
                        f"{round((sum(x['download'] for x in flow_rows) / max(sum(x['upload'] for x in flow_rows), 1)), 2)}:1"
                        if flow_rows else "0:1"
                    ),
                },
            },
        }

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

        total = Customer.objects.count()
        churn_qs = Customer.objects.filter(status__iexact="terminated", updated_at__gte=start)
        churned = churn_qs.count()
        churn_rate = round((churned / total) * 100, 2) if total else 0

        revenue_lost = _safe_float(
            Payment.objects.filter(status__iexact="completed", customer__in=churn_qs).aggregate(v=Sum("amount"))["v"]
        )

        payload = {
            "churnStats": {
                "churnRate": churn_rate,
                "churnedThisMonth": churned,
                "atRisk": 0,
                "revenueLost": revenue_lost,
                "avgLifetimeBeforeChurn": 0,
                "winbackRate": 0,
            },
            "churnReasons": [],
            "atRiskCustomers": [],
            "churnTrend": [{
                "month": timezone.now().strftime("%b %Y"),
                "churned": churned,
                "rate": churn_rate,
                "revenue": revenue_lost
            }],
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

        loc_raw = (
            Customer.objects.values("addresses__county")
            .annotate(count=Count("id"))
            .order_by("-count")[:20]
        )
        loc_total = sum(x["count"] for x in loc_raw) or 1
        by_loc = [{
            "location": x["addresses__county"] or "Unknown",
            "count": x["count"],
            "percentage": round((x["count"] / loc_total) * 100, 2),
        } for x in loc_raw]

        payload = {
            "customerStats": {
                "total": total,
                "active": active,
                "new": new,
                "churned": churned,
                "growthRate": round((new / max(total - new, 1)) * 100, 2) if total else 0,
                "retentionRate": round((active / total) * 100, 2) if total else 0,
                "avgLifetime": 0,
                "ltv": 0,
            },
            "customersByPlan": by_plan,
            "customersByLocation": by_loc,
            "cohortData": [],
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

        pay = Payment.objects.filter(status__iexact="completed", created_at__gte=start)
        totals = pay.aggregate(total=Sum("amount"), tx=Count("id"))
        total_amt = _safe_float(totals["total"])
        active_customers = Customer.objects.filter(status__iexact="active").count()
        arpu = round(total_amt / active_customers, 2) if active_customers else 0

        month_rows = (
            pay.annotate(m=TruncMonth("created_at"))
            .values("m")
            .annotate(revenue=Sum("amount"), transactions=Count("id"))
            .order_by("m")
        )
        monthly = [{
            "month": r["m"].strftime("%b %Y"),
            "revenue": _safe_float(r["revenue"]),
            "growth": 0,
            "transactions": r["transactions"],
        } for r in month_rows]

        pay_method = (
            pay.values("payment_method__name")
            .annotate(amount=Sum("amount"), count=Count("id"))
            .order_by("-amount")
        )
        method_total = sum(_safe_float(x["amount"]) for x in pay_method) or 1
        methods = [{
            "method": x["payment_method__name"] or "Unknown",
            "amount": _safe_float(x["amount"]),
            "percentage": round((_safe_float(x["amount"]) / method_total) * 100, 2),
            "count": x["count"],
        } for x in pay_method]

        # 2-query merge for revenueByPlan (fast enough, no N+1)
        plan_subs = {
            x["plan__name"]: x["subs"]
            for x in ServiceConnection.objects.filter(status__iexact="active")
            .values("plan__name").annotate(subs=Count("id"))
        }
        plan_rev_qs = (
            pay.values("invoice__plan__name")
            .annotate(revenue=Sum("amount"))
            .order_by("-revenue")
        )
        rev_total = sum(_safe_float(x["revenue"]) for x in plan_rev_qs) or 1
        by_plan = [{
            "plan": x["invoice__plan__name"] or "Unknown",
            "revenue": _safe_float(x["revenue"]),
            "subscribers": int(plan_subs.get(x["invoice__plan__name"], 0)),
            "percentage": round((_safe_float(x["revenue"]) / rev_total) * 100, 2),
        } for x in plan_rev_qs]

        payload = {
            "revenueStats": {
                "total": total_amt,
                "growth": 0,
                "arpu": arpu,
                "arpuGrowth": 0,
                "mrr": total_amt,
                "mrrGrowth": 0,
                "arr": total_amt * 12,
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

        usage = DataUsage.objects.filter(timestamp__gte=start)

        totals = usage.aggregate(
            down=Sum("download_mb"),
            up=Sum("upload_mb"),
            avg_daily=Avg(F("download_mb") + F("upload_mb")),
        )
        total_down = _safe_float(totals["down"])
        total_up = _safe_float(totals["up"])

        hour_rows = (
            usage.annotate(h=TruncHour("timestamp"))
            .values("h")
            .annotate(
                download=Sum("download_mb"),
                upload=Sum("upload_mb"),
                users=Count("customer", distinct=True)
            )
            .order_by("h")
        )

        hourly = [{
            "hour": r["h"].strftime("%H:00"),
            "download": _safe_float(r["download"]),
            "upload": _safe_float(r["upload"]),
            "users": int(r["users"] or 0),
        } for r in hour_rows]

        peak_hour = max(hourly, key=lambda x: x["download"] + x["upload"])["hour"] if hourly else None

        payload = {
            "usageStats": {
                "totalDownload": round(total_down / 1024, 2),  # GB
                "totalUpload": round(total_up / 1024, 2),      # GB
                "peakHour": peak_hour,
                "avgSessionDuration": 0,
                "heavyUsers": 0,
                "avgDailyUsage": round(_safe_float(totals["avg_daily"]) / 1024, 2),
            },
            "hourlyUsage": hourly,
            "usageByPlan": [],
            "topUsers": [],
        }
        cache.set(ck, payload, CACHE_TTL)
        return Response(payload)