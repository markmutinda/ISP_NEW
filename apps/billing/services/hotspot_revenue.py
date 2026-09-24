from decimal import Decimal

from django.db.models import Count, Sum


def completed_hotspot_payment_revenue(start, end):
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
        payment_date__lt=end,
    )
    totals = qs.aggregate(total=Sum("amount"), count=Count("id"))
    return {
        "revenue": totals["total"] or Decimal("0.00"),
        "count": totals["count"] or 0,
        "source": "completed_hotspot_payments",
    }
