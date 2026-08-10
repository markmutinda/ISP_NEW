from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from apps.core.permissions import IsAdminOrStaff
from django.utils import timezone
from django.core.cache import cache
from django.db import connection
from apps.billing.models.payment_models import Payment

TICKER_TTL = 20  # seconds — short enough to feel live, long enough to dedupe bursts


class RecentPaymentsTickerView(APIView):
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        cache_key = f"payments_ticker:{connection.schema_name}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        rows = (
            Payment.objects.filter(status='COMPLETED')  # Changed from status__iexact='completed'
            .only('amount', 'service_type', 'payment_date')
            .order_by('-payment_date')[:5]
        )
        data = [
            {
                'label': 'Hotspot' if p.service_type == 'HOTSPOT' else 'Fiber/DSL',
                'amount': float(p.amount),
                'time': timezone.localtime(p.payment_date).strftime('%-I:%M %p'),
            }
            for p in rows
        ]
        cache.set(cache_key, data, TICKER_TTL)
        return Response(data)