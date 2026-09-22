# apps/fup/services/delta_sync_service.py
import logging
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


class FUPDeltaSyncService:
    """
    Event-driven-ish usage accrual: pulls only radacct rows that changed
    since the last pass (indexed on acctupdatetime), computes the delta
    against FUPSessionCounter high-water marks, and atomically increments
    FUPUsageWindow + FUPUsageBucket in bulk. No per-user Python loop.
    """

    LOOKBACK_SECONDS = 180  # safety overlap in case a cycle runs slightly late

    def run(self) -> dict:
        now = timezone.now()
        cutoff = now - timedelta(seconds=self.LOOKBACK_SECONDS)

        with connection.cursor() as cur:
            # 1. One set-based query: every session that has moved since cutoff,
            #    joined against last-known counters (LEFT JOIN = new sessions get 0).
            cur.execute("""
                SELECT
                    ra.acctuniqueid,
                    ra.username,
                    COALESCE(ra.acctinputoctets, 0)  AS input_now,
                    COALESCE(ra.acctoutputoctets, 0) AS output_now,
                    COALESCE(fc.last_input_octets, 0)  AS input_prev,
                    COALESCE(fc.last_output_octets, 0) AS output_prev
                FROM radacct ra
                LEFT JOIN fup_fupsessioncounter fc ON fc.acctuniqueid = ra.acctuniqueid
                WHERE ra.acctupdatetime >= %s
                   OR (ra.acctstoptime IS NOT NULL AND ra.acctstoptime >= %s)
            """, [cutoff, cutoff])
            rows = cur.fetchall()

        if not rows:
            return {'sessions_touched': 0, 'windows_updated': 0}

        # 2. Compute deltas in Python (cheap — bounded by *active* sessions, not history)
        deltas_by_username: dict[str, int] = {}
        counter_upserts = []
        hour_start = now.replace(minute=0, second=0, microsecond=0)

        for acctuniqueid, username, in_now, out_now, in_prev, out_prev in rows:
            delta_in = max(0, in_now - in_prev)
            delta_out = max(0, out_now - out_prev)
            delta = delta_in + delta_out
            if delta:
                deltas_by_username[username] = deltas_by_username.get(username, 0) + delta
            counter_upserts.append((acctuniqueid, username, in_now, out_now))

        if not deltas_by_username:
            self._upsert_counters(counter_upserts)
            return {'sessions_touched': len(rows), 'windows_updated': 0}

        windows_updated = self._apply_deltas(deltas_by_username, hour_start, now)
        self._upsert_counters(counter_upserts)

        return {'sessions_touched': len(rows), 'windows_updated': windows_updated}

    def _upsert_counters(self, rows):
        if not rows:
            return
        with connection.cursor() as cur:
            cur.executemany("""
                INSERT INTO fup_fupsessioncounter
                    (acctuniqueid, username, last_input_octets, last_output_octets, last_synced_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (acctuniqueid) DO UPDATE SET
                    last_input_octets = EXCLUDED.last_input_octets,
                    last_output_octets = EXCLUDED.last_output_octets,
                    last_synced_at = NOW()
            """, rows)

    def _apply_deltas(self, deltas_by_username: dict, hour_start, now) -> int:
        """
        Bulk-increment FUPUsageWindow.total_bytes (F() expr, atomic) and
        upsert FUPUsageBucket for peak-hour accuracy. Returns windows touched.
        Also flags any window that just crossed its limit for immediate
        enforcement (returned via the caller's threshold check below).

        IMPORTANT: Creates missing windows for users whose period just rolled
        over or who have a first-ever accounting event — without this, their
        deltas would be silently dropped and FUP would stop enforcing.
        """
        from django.db.models import F, Q
        from apps.fup.models import FUPUsageWindow, FUPUsageBucket

        usernames = list(deltas_by_username.keys())
        windows = list(
            FUPUsageWindow.objects.filter(
                period_start__lte=now, period_end__gt=now,
            ).filter(
                Q(service_connection__customer__radius_credentials__username__in=usernames) |
                Q(hotspot_session__access_code__in=usernames)
            ).select_related(
                'policy',
                'service_connection__customer__radius_credentials',
                'hotspot_session',
            )
        )
        windows_by_username = {}
        for w in windows:
            uname = _window_username(w)
            if uname:
                windows_by_username[uname] = w

        # Usernames with a delta but no window covering `now` — new period or
        # first-ever accounting event. Without this, their usage is dropped.
        missing = [u for u in usernames if u not in windows_by_username]
        if missing:
            windows_by_username.update(self._create_missing_windows(missing, now))

        touched = 0
        crossed_ids = []
        for uname, w in windows_by_username.items():
            delta = deltas_by_username.get(uname)
            if not delta:
                continue
            was_over = w.total_bytes > w.limit_bytes
            FUPUsageWindow.objects.filter(pk=w.pk).update(
                total_bytes=F('total_bytes') + delta,
                last_accounting_update_at=now,
            )
            touched += 1
            FUPUsageBucket.objects.update_or_create(
                policy=w.policy, username=uname, hour_start=hour_start,
                defaults={
                    'service_connection': w.service_connection,
                    'hotspot_session': w.hotspot_session,
                },
            )
            FUPUsageBucket.objects.filter(
                policy=w.policy, username=uname, hour_start=hour_start,
            ).update(bytes_total=F('bytes_total') + delta)

            if not was_over and (w.total_bytes + delta) > w.limit_bytes:
                crossed_ids.append(w.pk)

        if crossed_ids:
            self._enforce_crossed(crossed_ids)
        return touched

    def _create_missing_windows(self, usernames, now):
        """
        Create FUPUsageWindow rows for usernames that have a delta but no
        window covering `now`. This is what makes period rollover safe:
        without it, once a period resets there's no window for the new
        period and every subsequent delta is silently dropped forever.

        Returns: {username: window}
        """
        from apps.customers.models import ServiceConnection
        from apps.radius.models import CustomerRadiusCredentials
        from apps.billing.models.hotspot_models import HotspotSession
        from apps.fup.models import FUPUsageWindow
        from apps.fup.services.usage_service import FUPUsageService

        usage_service = FUPUsageService()
        result = {}

        creds_map = {
            c.username: c for c in
            CustomerRadiusCredentials.objects.filter(username__in=usernames).select_related('customer')
        }
        hotspot_map = {
            s.access_code: s for s in
            HotspotSession.objects.filter(access_code__in=usernames, status='active').select_related('plan')
        }

        for uname in usernames:
            cred = creds_map.get(uname)
            if cred:
                service = ServiceConnection.objects.filter(
                    customer=cred.customer, status='ACTIVE', plan__isnull=False
                ).select_related('plan').first()
                if not service:
                    continue
                policy = usage_service.get_active_policy_for_service(service)
                if not policy:
                    continue
                period_start, period_end = usage_service.resolve_window(
                    policy, service_connection=service, now=now
                )
                window, _ = FUPUsageWindow.objects.get_or_create(
                    policy=policy, service_connection=service,
                    period_start=period_start, period_end=period_end,
                    defaults={
                        'plan': service.plan,
                        'customer': cred.customer,
                        'limit_bytes': policy.limit_bytes,
                    },
                )
                result[uname] = window
                continue

            session = hotspot_map.get(uname)
            if session:
                policy = usage_service.get_active_policy_for_hotspot_session(session)
                if not policy:
                    continue
                activation = session.activated_at or session.created_at
                if policy.reset_period == 'SUBSCRIPTION' and session.plan:
                    total_minutes = session.plan.total_validity_minutes or 60
                    period_start, period_end = usage_service._resolve_subscription_window_for_hotspot(
                        activation, total_minutes, now
                    )
                else:
                    period_start, period_end = usage_service.resolve_window(
                        policy, activation_date=activation, now=now
                    )
                window, _ = FUPUsageWindow.objects.get_or_create(
                    policy=policy, hotspot_session=session,
                    period_start=period_start, period_end=period_end,
                    defaults={
                        'hotspot_plan': session.plan,
                        'limit_bytes': policy.limit_bytes,
                    },
                )
                result[uname] = window

        return result

    def _enforce_crossed(self, window_ids):
        """Only the handful of windows that just crossed the line get throttled — not everyone."""
        from apps.fup.models import FUPUsageWindow
        from apps.fup.services.enforcement_service import FUPEnforcementService

        service = FUPEnforcementService()
        for w in FUPUsageWindow.objects.filter(pk__in=window_ids).select_related(
            'service_connection', 'hotspot_session', 'policy'
        ):
            if w.service_connection:
                service.evaluate_service(w.service_connection)
            elif w.hotspot_session:
                service.evaluate_hotspot_session(w.hotspot_session)


def _window_username(w):
    """
    Resolve the username a window belongs to. Null-safe: returns None if the
    user has no RADIUS credentials yet (rather than crashing).
    """
    if w.service_connection_id:
        creds = getattr(
            getattr(w.service_connection, 'customer', None),
            'radius_credentials',
            None,
        )
        return creds.username if creds else None
    if w.hotspot_session_id:
        return w.hotspot_session.access_code
    return None