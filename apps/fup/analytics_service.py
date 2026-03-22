from datetime import timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.fup.models import FUPPolicy, FUPViolation


class FUPAnalyticsService:
    def get_violation_trends(self, days=30):
        start = timezone.now() - timedelta(days=days)
        qs = (
            FUPViolation.objects
            .filter(occurred_at__gte=start)
            .annotate(day=TruncDate('occurred_at'))
            .values('day')
            .annotate(count=Count('id'))
            .order_by('day')
        )

        return [
            {
                'date': item['day'].isoformat(),
                'count': item['count'],
            }
            for item in qs
        ]

    def get_top_violators_this_month(self, limit=10):
        now = timezone.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        qs = (
            FUPViolation.objects
            .filter(occurred_at__gte=month_start)
            .values(
                'customer_id',
                'customer__customer_code',
                'customer__user__first_name',
                'customer__user__last_name',
            )
            .annotate(violations=Count('id'))
            .order_by('-violations')[:limit]
        )

        data = []
        for row in qs:
            full_name = f"{row.get('customer__user__first_name', '')} {row.get('customer__user__last_name', '')}".strip()
            data.append({
                'customer_id': row['customer_id'],
                'customer_code': row['customer__customer_code'],
                'name': full_name,
                'violations': row['violations'],
            })
        return data

    def get_policy_distribution(self):
        qs = (
            FUPPolicy.objects
            .filter(is_active=True)
            .annotate(
                users=Count(
                    'plan_links__plan__service_connections',
                    distinct=True
                )
            )
            .values('id', 'name', 'users')
            .order_by('-users', 'name')
        )

        return [
            {
                'policy_id': row['id'],
                'policy_name': row['name'],
                'users': row['users'],
            }
            for row in qs
        ]

    def overview(self):
        return {
            'violation_trends': self.get_violation_trends(),
            'top_violators_this_month': self.get_top_violators_this_month(),
            'policy_distribution': self.get_policy_distribution(),
        }