"""
RADIUS usage analytics (powers /admin/usage).

Traffic is attributed to a session's last activity time
(Coalesce(acctstoptime, acctupdatetime, acctstarttime)), because FreeRADIUS
updates radacct rows in place and keeps no per-interval deltas.
Direction: acctinputoctets = user upload, acctoutputoctets = user download.
"""
import logging
from datetime import timedelta

from django.core.cache import cache
from django.db import connection
from django.db.models import (
    BigIntegerField, Count, DateTimeField, F, Max, Min, Q, Sum, Value,
)
from django.db.models.functions import Coalesce, ExtractHour, TruncDay, TruncHour
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import HasCompanyAccess, HasRoleAccessPolicy
from .models import CustomerRadiusCredentials, RadAcct

logger = logging.getLogger(__name__)

RANGES = {'24h': ('hour', 24), '7d': ('day', 7), '30d': ('day', 30)}
MAX_PLAN_ROWS = 20000


# ───────────────────────── helpers ─────────────────────────

def _window(range_key):
    """Bucket-aligned window. Current = [start, end), previous = [prev_start, start)."""
    unit, n = RANGES[range_key]
    local_now = timezone.localtime(timezone.now())
    if unit == 'hour':
        step = timedelta(hours=1)
        last = local_now.replace(minute=0, second=0, microsecond=0)
    else:
        step = timedelta(days=1)
        last = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = last - step * (n - 1)
    end = last + step
    prev_start = start - step * n
    return unit, n, step, start, end, prev_start


def _total():
    z = lambda f: Coalesce(f, Value(0), output_field=BigIntegerField())
    return z('acctinputoctets') + z('acctoutputoctets')


def _base_qs(since):
    """
    Index-friendly pre-filter (acctstarttime / acctstoptime are indexed), then the
    exact last-activity annotation. Callers add last_activity range filters.
    """
    return (
        RadAcct.objects
        .filter(
            Q(acctstarttime__gte=since)
            | Q(acctstoptime__isnull=True)
            | Q(acctstoptime__gte=since)
        )
        .annotate(
            last_activity=Coalesce(
                'acctstoptime', 'acctupdatetime', 'acctstarttime',
                output_field=DateTimeField(),
            )
        )
    )


def _chunks(seq, size=2000):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _pct_change(cur, prev):
    if not prev:
        return None
    return round(((cur - prev) / prev) * 100, 1)


def _plan_map(usernames):
    """username -> plan name (PPPoE via active service, hotspot via latest session)."""
    from apps.customers.models import ServiceConnection
    from apps.billing.models.hotspot_models import HotspotSession

    names = list(usernames)
    out = {}
    for chunk in _chunks(names):
        rows = (
            ServiceConnection.objects
            .filter(
                customer__radius_credentials__username__in=chunk,
                status='ACTIVE',
                plan__isnull=False,
            )
            .values_list('customer__radius_credentials__username', 'plan__name')
        )
        for uname, plan_name in rows:
            out.setdefault(uname, plan_name)

    missing = [n for n in names if n not in out]
    for chunk in _chunks(missing):
        rows = (
            HotspotSession.objects
            .filter(access_code__in=chunk)
            .order_by('access_code', '-created_at')
            .distinct('access_code')
            .values_list('access_code', 'plan__name')
        )
        for code, plan_name in rows:
            out.setdefault(code, plan_name)
    return out


# ───────────────────────── analytics ─────────────────────────

class RadiusUsageAnalyticsView(APIView):
    """GET /api/v1/radius/usage/analytics/?range=24h|7d|30d"""
    permission_classes = [IsAuthenticated, HasCompanyAccess, HasRoleAccessPolicy]
    required_rbac_paths = ("/admin/usage", "/admin/radius")

    def get(self, request):
        rng = request.query_params.get('range', '7d')
        if rng not in RANGES:
            rng = '7d'

        cache_key = f"radius:usage_analytics:v1:{connection.schema_name}:{rng}"
        data = cache.get(cache_key)
        if data is None:
            data = self._build(rng)
            cache.set(cache_key, data, 60)
        return Response(data)

    def _build(self, rng):
        unit, n, step, start, end, prev_start = _window(rng)
        tz = timezone.get_current_timezone()

        cur = Q(last_activity__gte=start, last_activity__lt=end)
        prev = Q(last_activity__gte=prev_start, last_activity__lt=start)

        # 1) KPI totals (current + previous period) in ONE scan
        agg = _base_qs(prev_start).aggregate(
            dl=Sum('acctoutputoctets', filter=cur),
            ul=Sum('acctinputoctets', filter=cur),
            pdl=Sum('acctoutputoctets', filter=prev),
            pul=Sum('acctinputoctets', filter=prev),
            users=Count('username', distinct=True, filter=cur),
        )
        dl, ul = int(agg['dl'] or 0), int(agg['ul'] or 0)
        pdl, pul = int(agg['pdl'] or 0), int(agg['pul'] or 0)
        total, prev_total = dl + ul, pdl + pul
        users = int(agg['users'] or 0)

        cur_qs = _base_qs(start).filter(cur)

        # 2) Time series (grouped in SQL, gaps filled below)
        trunc = TruncHour if unit == 'hour' else TruncDay
        rows = (
            cur_qs
            .annotate(bucket=trunc('last_activity', tzinfo=tz))
            .values('bucket')
            .annotate(dl=Sum('acctoutputoctets'), ul=Sum('acctinputoctets'))
            .order_by('bucket')
        )
        fmt = '%Y-%m-%d %H' if unit == 'hour' else '%Y-%m-%d'
        found = {timezone.localtime(r['bucket']).strftime(fmt): r for r in rows}
        series, cursor = [], start
        for _ in range(n):
            r = found.get(cursor.strftime(fmt))
            series.append({
                'label': cursor.strftime('%H:00' if unit == 'hour' else '%d %b'),
                'download': int(r['dl'] or 0) if r else 0,
                'upload': int(r['ul'] or 0) if r else 0,
            })
            cursor += step

        # 3) Peak hour of day
        peak = (
            cur_qs
            .annotate(h=ExtractHour('last_activity', tzinfo=tz))
            .values('h')
            .annotate(b=Sum(_total()))
            .order_by('-b')
            .first()
        )
        peak_hour = peak['h'] if peak and peak['b'] else None

        # 4) Usage by plan (bounded: top MAX_PLAN_ROWS users by traffic)
        user_rows = list(
            cur_qs.values('username')
            .annotate(b=Sum(_total()))
            .order_by('-b')[:MAX_PLAN_ROWS]
        )
        plan_of = _plan_map([r['username'] for r in user_rows if r['username']])
        by_plan, grand = {}, 0
        for r in user_rows:
            b = int(r['b'] or 0)
            grand += b
            name = plan_of.get(r['username']) or 'Unassigned'
            by_plan[name] = by_plan.get(name, 0) + b
        ranked = sorted(by_plan.items(), key=lambda kv: kv[1], reverse=True)
        head, tail = ranked[:4], ranked[4:]
        if tail:
            head.append(('Other plans', sum(b for _, b in tail)))
        plans = [
            {'name': name, 'bytes': b, 'percent': round(b / grand * 100, 1) if grand else 0}
            for name, b in head
        ]

        return {
            'range': rng,
            'granularity': unit,
            'summary': {
                'total': total,
                'download': dl,
                'upload': ul,
                'active_sessions': RadAcct.objects.filter(acctstoptime__isnull=True).count(),
                'unique_users': users,
                'avg_per_user': int(total / users) if users else 0,
                'peak_hour': peak_hour,
                'changes': {
                    'total': _pct_change(total, prev_total),
                    'download': _pct_change(dl, pdl),
                    'upload': _pct_change(ul, pul),
                },
            },
            'series': series,
            'plans': plans,
        }


# ───────────────────────── top consumers ─────────────────────────

class RadiusUsageConsumersView(APIView):
    """GET /api/v1/radius/usage/consumers/?range=7d&page=1&page_size=10&q=..."""
    permission_classes = [IsAuthenticated, HasCompanyAccess, HasRoleAccessPolicy]
    required_rbac_paths = ("/admin/usage", "/admin/radius")

    def get(self, request):
        p = request.query_params
        rng = p.get('range', '7d')
        if rng not in RANGES:
            rng = '7d'
        try:
            page = max(1, int(p.get('page', 1)))
            page_size = min(50, max(5, int(p.get('page_size', 10))))
        except (TypeError, ValueError):
            page, page_size = 1, 10
        q = (p.get('q') or '').strip()[:64]

        _, _, _, start, end, _ = _window(rng)
        qs = _base_qs(start).filter(last_activity__gte=start, last_activity__lt=end)

        if q:
            matched = list(
                CustomerRadiusCredentials.objects.filter(
                    Q(username__icontains=q)
                    | Q(customer__customer_code__icontains=q)
                    | Q(customer__user__first_name__icontains=q)
                    | Q(customer__user__last_name__icontains=q)
                    | Q(customer__user__phone_number__icontains=q)
                ).values_list('username', flat=True)[:500]
            )
            qs = qs.filter(Q(username__icontains=q) | Q(username__in=matched))

        zero = lambda f: Coalesce(F(f), Value(0), output_field=BigIntegerField())
        grouped = (
            qs.values('username')
            .annotate(
                dl=Sum('acctoutputoctets'),
                ul=Sum('acctinputoctets'),
                sessions=Count('radacctid'),
                online=Count('radacctid', filter=Q(acctstoptime__isnull=True)),
                open_since=Min('acctstarttime', filter=Q(acctstoptime__isnull=True)),
                last_seen=Max('last_activity'),
            )
            .annotate(total=zero('dl') + zero('ul'))
            .order_by('-total', 'username')
        )

        count = qs.values('username').distinct().count()
        offset = (page - 1) * page_size
        page_rows = list(grouped[offset:offset + page_size])

        usernames = [r['username'] for r in page_rows]
        identity = {}
        for c in (
            CustomerRadiusCredentials.objects
            .filter(username__in=usernames)
            .select_related('customer__user')
        ):
            cust = c.customer
            identity[c.username] = {
                'name': (cust.full_name if cust else None) or c.username,
                'code': getattr(cust, 'customer_code', '') or '',
                'type': 'pppoe',
            }
        plan_of = _plan_map(usernames)

        now = timezone.now()
        results = []
        for r in page_rows:
            ident = identity.get(r['username']) or {
                'name': r['username'], 'code': '', 'type': 'hotspot',
            }
            is_online = (r['online'] or 0) > 0
            results.append({
                'username': r['username'],
                'name': ident['name'],
                'code': ident['code'] or r['username'],
                'type': ident['type'],
                'plan': plan_of.get(r['username']) or '—',
                'download': int(r['dl'] or 0),
                'upload': int(r['ul'] or 0),
                'total': int(r['total'] or 0),
                'sessions': r['sessions'],
                'online': is_online,
                'uptime_seconds': (
                    int((now - r['open_since']).total_seconds())
                    if is_online and r['open_since'] else None
                ),
                'last_seen': r['last_seen'].isoformat() if r['last_seen'] else None,
            })

        return Response({
            'count': count,
            'page': page,
            'page_size': page_size,
            'total_pages': max(1, (count + page_size - 1) // page_size),
            'results': results,
        })