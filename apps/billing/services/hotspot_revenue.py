from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone


def completed_hotspot_payment_revenue(start, end=None):
    """
    Return completed hotspot payment revenue for [start, end).

    This matches the reports revenue split source: completed payments classified
    permanently with service_type=HOTSPOT.
    """
    from apps.billing.models import Payment

    qs = Payment.objects.filter(
        status__iexact="completed",
        service_type="HOTSPOT",
        payment_date__gte=start,
    )
    if end is not None:
        qs = qs.filter(payment_date__lt=end)
    totals = qs.aggregate(total=Sum("amount"), count=Count("id"))
    return {
        "revenue": totals["total"] or Decimal("0.00"),
        "count": totals["count"] or 0,
        "source": "completed_hotspot_payments",
    }


def rolling_reports_hotspot_payment_revenue(cycle, days=30):
    """
    Return hotspot revenue using the same default reporting window as the
    PPPoE vs Hotspot Daily Revenue chart, constrained by a newer billing cycle.
    """
    now = timezone.now()
    report_start = now - timedelta(days=days)
    start = max(cycle.start_date, report_start)
    end = min(cycle.end_date, now) if cycle.end_date else now
    details = completed_hotspot_payment_revenue(start, end)
    details.update({
        "window_start": start,
        "window_end": end,
        "window_days": days,
    })
    return details
