"""
Subscription-period usage accounting.

Usage is anchored to the START OF THE CURRENT SUBSCRIPTION PERIOD (never to a
session start), so it accumulates across disconnects/reconnects and only resets
when the subscription is renewed.

Anchors:
  PPPoE   -> CustomerRadiusCredentials.subscription_activated_at (fallback: created_at)
  Hotspot -> latest HotspotSession.activated_at for that access code
"""
from typing import Dict, Iterable, List, Optional

from django.db import connection
from django.db.models import Q
from django.utils import timezone

from apps.radius.models import CustomerRadiusCredentials

MAX_USERNAMES = 500


def format_bytes(total_bytes) -> str:
    mb = (total_bytes or 0) / (1024 * 1024)
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.2f} MB"


def get_period_starts(usernames: List[str], creds_map: Optional[dict] = None) -> dict:
    """username -> period start. Usernames without an anchor are omitted."""
    starts = {}

    if creds_map is None:
        creds_map = {
            c.username: c
            for c in CustomerRadiusCredentials.objects
            .filter(username__in=usernames)
            .only('username', 'subscription_activated_at', 'created_at')
        }
    for uname, cred in creds_map.items():
        anchor = cred.subscription_activated_at or cred.created_at
        if anchor:
            starts[uname] = anchor

    hotspot_names = [u for u in usernames if u not in starts]
    if hotspot_names:
        from apps.billing.models.hotspot_models import HotspotSession
        rows = (
            HotspotSession.objects
            .filter(
                access_code__in=hotspot_names,
                activated_at__isnull=False,
                status__in=('active', 'paid', 'expired'),
            )
            .order_by('access_code', '-activated_at')
            .distinct('access_code')          # PostgreSQL DISTINCT ON
            .values_list('access_code', 'activated_at')
        )
        starts.update(dict(rows))

    return starts


def get_period_usage(usernames: Iterable[str], creds_map: Optional[dict] = None) -> Dict[str, int]:
    """username -> total bytes (in+out) since that user's current period start. 1 SQL query."""
    names = list(dict.fromkeys(u for u in usernames if u))[:MAX_USERNAMES]
    if not names:
        return {}

    starts = get_period_starts(names, creds_map)
    fallback = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    period_starts = [starts.get(n, fallback) for n in names]

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT ra.username,
                   COALESCE(SUM(ra.acctinputoctets), 0) + COALESCE(SUM(ra.acctoutputoctets), 0)
            FROM unnest(%s::text[], %s::timestamptz[]) AS p(username, period_start)
            JOIN radacct ra
              ON ra.username = p.username
             AND ra.acctstarttime >= p.period_start
            GROUP BY ra.username
            """,
            [names, period_starts],
        )
        totals = dict(cursor.fetchall())

    return {n: int(totals.get(n, 0)) for n in names}


def build_online_context(rows) -> dict:
    """
    One-shot prefetch for a page of open radacct rows.
    Replaces the per-row queries in OnlineUserSerializer.
    """
    from apps.billing.models.hotspot_models import HotspotSession
    from apps.network.models import Router

    names = list({r.username for r in rows if r.username})
    if not names:
        return {'usage': {}, 'creds': {}, 'hotspot': {}, 'routers': {}}

    creds = {
        c.username: c
        for c in CustomerRadiusCredentials.objects
        .filter(username__in=names)
        .select_related('customer__user')
    }
    hotspot = {
        s.access_code: s
        for s in HotspotSession.objects
        .filter(access_code__in=names)
        .select_related('hotspot_client')
        .order_by('access_code', '-created_at')
        .distinct('access_code')
    }

    ips = {str(r.nasipaddress) for r in rows if r.nasipaddress}
    routers = {}
    if ips:
        for rt in (
            Router.objects
            .filter(Q(vpn_ip_address__in=ips) | Q(ip_address__in=ips))
            .only('name', 'ip_address', 'vpn_ip_address')
        ):
            for ip in (rt.vpn_ip_address, rt.ip_address):
                if ip:
                    routers.setdefault(str(ip), rt.name)

    usage = {n: format_bytes(b) for n, b in get_period_usage(names, creds).items()}
    return {'usage': usage, 'creds': creds, 'hotspot': hotspot, 'routers': routers}