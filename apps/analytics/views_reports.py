"""
Reports & Analytics API - serves data for the 4-tab Reports page.
Tabs: Overview, Financial, Users, Network
"""
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from django.utils import timezone
from django.db.models import Sum, Count, Avg, Max, Q
from django.db.models.functions import TruncDay, TruncHour, ExtractHour
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser
import logging

from apps.customers.models import Customer
from apps.billing.models.payment_models import Payment
from apps.bandwidth.models import DataUsage

logger = logging.getLogger(__name__)

# ──────────────────────────────────────
# Helpers
# ──────────────────────────────────────

def _completed_payments(**extra_filters):
    return Payment.objects.filter(status='completed', **extra_filters)


def _period_revenue(start, end):
    """Return (total_amount, transaction_count) for completed payments in [start, end)."""
    qs = _completed_payments(payment_date__gte=start, payment_date__lt=end)
    agg = qs.aggregate(total=Sum('amount'), count=Count('id'))
    return float(agg['total'] or 0), agg['count'] or 0


class ReportsDataView(APIView):
    """
    GET /api/v1/analytics/reports/
    Returns all data needed by the 4-tab Reports & Analytics page.
    Query params:
        time_range: 7d | 30d | 90d (default 30d) — controls user & network charts
    """
    permission_classes = [IsAdminUser]

    def get(self, request):
        time_range = request.query_params.get('time_range', '30d')
        days = {'7d': 7, '30d': 30, '90d': 90}.get(time_range, 30)
        now = timezone.now()

        data = {
            'overview': self._overview(now, days),
            'financial': self._financial(now),
            'users': self._users(now, days),
            'network': self._network(now, days),
            'time_range': time_range,
        }
        return Response(data)

    # ──────────────────────────────────────
    # OVERVIEW TAB
    # ──────────────────────────────────────
    def _overview(self, now, days):
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        week_start = today_start - timedelta(days=today_start.weekday())
        last_week_start = week_start - timedelta(weeks=1)
        month_start = today_start.replace(day=1)
        last_month_start = (month_start - timedelta(days=1)).replace(day=1)

        # Revenue cards
        today_rev, today_count = _period_revenue(today_start, now)
        yesterday_rev, _ = _period_revenue(yesterday_start, today_start)
        today_change = round(((today_rev - yesterday_rev) / yesterday_rev * 100), 1) if yesterday_rev else 0

        week_rev, week_count = _period_revenue(week_start, now)
        last_week_rev, _ = _period_revenue(last_week_start, week_start)
        week_change = round(((week_rev - last_week_rev) / last_week_rev * 100), 1) if last_week_rev else 0

        month_rev, month_count = _period_revenue(month_start, now)
        last_month_rev, _ = _period_revenue(last_month_start, month_start)
        month_change = round(((month_rev - last_month_rev) / last_month_rev * 100), 1) if last_month_rev else 0

        # Hourly revenue distribution (today)
        hourly = self._hourly_revenue(today_start, now)

        # User registrations (last N days)
        user_regs = self._daily_registrations(now, days)

        # Network data flow (last N days)
        network_flow = self._daily_network(now, days)

        return {
            'today_revenue': today_rev,
            'today_change': today_change,
            'week_revenue': week_rev,
            'week_change': week_change,
            'month_revenue': month_rev,
            'month_change': month_change,
            'total_transactions_today': today_count,
            'hourly_revenue': hourly,
            'user_registrations': user_regs,
            'network_data_flow': network_flow,
        }

    # ──────────────────────────────────────
    # FINANCIAL TAB
    # ──────────────────────────────────────
    def _financial(self, now):
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        week_start = today_start - timedelta(days=today_start.weekday())
        last_week_start = week_start - timedelta(weeks=1)
        month_start = today_start.replace(day=1)
        last_month_start = (month_start - timedelta(days=1)).replace(day=1)

        today_rev, today_count = _period_revenue(today_start, now)
        yesterday_rev, yesterday_count = _period_revenue(yesterday_start, today_start)
        week_rev, week_count = _period_revenue(week_start, now)
        last_week_rev, last_week_count = _period_revenue(last_week_start, week_start)
        month_rev, month_count = _period_revenue(month_start, now)
        last_month_rev, last_month_count = _period_revenue(last_month_start, month_start)

        growth_rate = round(((month_rev - last_month_rev) / last_month_rev * 100), 1) if last_month_rev else 0

        # Hourly revenue for today (full-size chart)
        hourly = self._hourly_revenue(today_start, now)

        return {
            'income_comparison': {
                'today': {'amount': today_rev, 'transactions': today_count},
                'yesterday': {'amount': yesterday_rev, 'transactions': yesterday_count},
                'this_week': {'amount': week_rev, 'transactions': week_count},
                'last_week': {'amount': last_week_rev, 'transactions': last_week_count},
            },
            'monthly_performance': {
                'this_month': {'amount': month_rev, 'transactions': month_count},
                'last_month': {'amount': last_month_rev, 'transactions': last_month_count},
                'growth_rate': growth_rate,
            },
            'hourly_revenue': hourly,
        }

    # ──────────────────────────────────────
    # USERS TAB
    # ──────────────────────────────────────
    def _users(self, now, days):
        regs = self._daily_registrations(now, days)
        total = sum(r['count'] for r in regs)
        avg_per_day = round(total / max(len(regs), 1), 1)
        peak = max(regs, key=lambda r: r['count']) if regs else {'date': '-', 'count': 0}

        return {
            'registration_trends': regs,
            'summary': {
                'total_registrations': total,
                'avg_per_day': avg_per_day,
                'peak_day': f"{peak['count']} users",
            },
        }

    # ──────────────────────────────────────
    # NETWORK TAB
    # ──────────────────────────────────────
    def _network(self, now, days):
        daily = self._daily_network(now, days)

        total_upload = sum(d['upload'] for d in daily)
        total_download = sum(d['download'] for d in daily)
        total_usage = total_upload + total_download
        avg_daily = round(total_usage / max(len(daily), 1), 1)
        peak_day = max(daily, key=lambda d: d['upload'] + d['download']) if daily else None
        peak_val = round(peak_day['upload'] + peak_day['download'], 1) if peak_day else 0
        ratio = f"{round(total_download / total_upload, 1)}:1" if total_upload > 0 else "N/A"

        return {
            'daily_usage': daily,
            'usage_summary': {
                'total_upload': round(total_upload, 2),
                'total_download': round(total_download, 2),
                'total_usage': round(total_usage, 2),
            },
            'performance': {
                'peak_usage_day': f"{peak_val} GB",
                'avg_daily_usage': f"{avg_daily} GB",
                'download_upload_ratio': ratio,
            },
        }

    # ──────────────────────────────────────
    # Shared sub-queries
    # ──────────────────────────────────────
    def _hourly_revenue(self, start, end):
        """Hourly revenue distribution between start and end."""
        qs = _completed_payments(
            payment_date__gte=start, payment_date__lt=end,
        ).annotate(
            hour=ExtractHour('payment_date'),
        ).values('hour').annotate(
            total=Sum('amount'),
        ).order_by('hour')

        hour_map = {h['hour']: float(h['total'] or 0) for h in qs}

        result = []
        labels = [
            '12AM', '1AM', '2AM', '3AM', '4AM', '5AM',
            '6AM', '7AM', '8AM', '9AM', '10AM', '11AM',
            '12PM', '1PM', '2PM', '3PM', '4PM', '5PM',
            '6PM', '7PM', '8PM', '9PM', '10PM', '11PM',
        ]
        for h in range(24):
            amount = hour_map.get(h, 0)
            # Classify: peak (17-22), business (8-17), off (rest)
            if 17 <= h < 22:
                cat = 'peak_hours'
            elif 8 <= h < 17:
                cat = 'business_hours'
            else:
                cat = 'off_hours'
            result.append({
                'hour': labels[h],
                'peak_hours': amount if cat == 'peak_hours' else 0,
                'business_hours': amount if cat == 'business_hours' else 0,
                'off_hours': amount if cat == 'off_hours' else 0,
            })
        return result

    def _daily_registrations(self, now, days):
        """Daily customer registrations over the last N days."""
        start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        qs = Customer.objects.filter(
            created_at__gte=start,
        ).annotate(
            day=TruncDay('created_at'),
        ).values('day').annotate(
            count=Count('id'),
        ).order_by('day')

        day_map = {d['day'].date(): d['count'] for d in qs}

        result = []
        cursor = start.date()
        end = now.date()
        while cursor <= end:
            result.append({
                'date': cursor.strftime('%b %d'),
                'count': day_map.get(cursor, 0),
            })
            cursor += timedelta(days=1)
        return result

    def _daily_network(self, now, days):
        """Daily upload/download in GB over the last N days.

        Uses DataUsage if data exists, otherwise falls back to
        RADIUS accounting (radacct).
        """
        start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

        # Try DataUsage first
        qs = DataUsage.objects.filter(
            period_start__gte=start,
        ).annotate(
            day=TruncDay('period_start'),
        ).values('day').annotate(
            upload=Sum('upload_bytes'),
            download=Sum('download_bytes'),
        ).order_by('day')

        if qs.exists():
            day_map = {
                d['day'].date(): {
                    'upload': round((d['upload'] or 0) / (1024 ** 3), 2),
                    'download': round((d['download'] or 0) / (1024 ** 3), 2),
                }
                for d in qs
            }
        else:
            # Fallback: RADIUS accounting
            try:
                from apps.radius.models import RadiusAccounting
                rqs = RadiusAccounting.objects.filter(
                    acctstarttime__gte=start,
                ).annotate(
                    day=TruncDay('acctstarttime'),
                ).values('day').annotate(
                    upload=Sum('acctinputoctets'),
                    download=Sum('acctoutputoctets'),
                ).order_by('day')

                day_map = {
                    d['day'].date(): {
                        'upload': round((d['upload'] or 0) / (1024 ** 3), 2),
                        'download': round((d['download'] or 0) / (1024 ** 3), 2),
                    }
                    for d in rqs
                }
            except Exception:
                day_map = {}

        result = []
        cursor = start.date()
        end = now.date()
        while cursor <= end:
            vals = day_map.get(cursor, {'upload': 0, 'download': 0})
            result.append({
                'date': cursor.strftime('%b %d'),
                'upload': vals['upload'],
                'download': vals['download'],
            })
            cursor += timedelta(days=1)
        return result
