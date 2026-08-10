from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from apps.core.permissions import IsAdminOrStaff
from django.utils import timezone
from apps.billing.models.payment_models import Payment

class RecentPaymentsTickerView(APIView):
    permission_classes = [IsAuthenticated, IsAdminOrStaff]

    def get(self, request):
        rows = (
            Payment.objects.filter(status__iexact='completed')
            .only('amount', 'service_type', 'payment_date')
            .order_by('-payment_date')[:5]
        )
        return Response([
            {
                'label': 'Hotspot' if p.service_type == 'HOTSPOT' else 'Fiber/DSL',
                'amount': float(p.amount),
                'time': timezone.localtime(p.payment_date).strftime('%-I:%M %p'),
            }
            for p in rows
        ])