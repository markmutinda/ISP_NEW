import logging

from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.fup.models import FUPPolicy, FUPPolicyPlan, FUPThrottleState, FUPViolation

logger = logging.getLogger(__name__)

RATE_ATTR = 'Mikrotik-Rate-Limit'
CHUNK = 1000
SNAPSHOT_SIZE = 200
SNAPSHOT_TTL = 900


def snapshot_key(schema):
    return f'fup:snapshot:{schema}'


def lastrun_key(schema):
    return f'fup:lastrun:{schema}'


def _rate(down_mbps, up_mbps):
    return f'{int(up_mbps) * 1000}k/{int(down_mbps) * 1000}k'


class FUPEngine:
    """
    Subscription-period FUP. Usage = SUM(radacct octets) since the user's
    subscription_activated_at. Must run inside the tenant schema.
    """

    def __init__(self):
        self.schema = connection.schema_name

    # ───────────────────────── usage ─────────────────────────
    @staticmethod
    def _usage(pairs):
        """pairs: [(username, since)] -> {username: total_bytes}. 1 query per 1000 users."""
        pairs = list({u: s for u, s in pairs}.items())  # dedupe usernames (avoids double SUM)
        out = {}
        for i in range(0, len(pairs), CHUNK):
            chunk = pairs[i:i + CHUNK]
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT ra.username,
                           COALESCE(SUM(ra.acctinputoctets), 0) + COALESCE(SUM(ra.acctoutputoctets), 0)
                    FROM unnest(%s::text[], %s::timestamptz[]) AS p(username, since)
                    JOIN radacct ra
                      ON ra.username = p.username
                     AND ra.acctstarttime >= p.since
                    GROUP BY ra.username
                    """,
                    [[c[0] for c in chunk], [c[1] for c in chunk]],
                )
                out.update(dict(cur.fetchall()))
        return out

    # ───────────────────────── evaluate ─────────────────────────
    def evaluate(self):
        """Returns (rows, policies). Read-only, no side effects."""
        from apps.customers.models import ServiceConnection

        now = timezone.now()
        policies = {p.id: p for p in FUPPolicy.objects.filter(is_active=True, status='ACTIVE')}
        if not policies:
            return [], policies

        plan_policy = {}
        links = (
            FUPPolicyPlan.objects
            .filter(is_active=True, policy_id__in=list(policies))
            .order_by('linked_at')
            .values_list('plan_id', 'policy_id')
        )
        for plan_id, policy_id in links:
            plan_policy.setdefault(plan_id, policy_id)
        if not plan_policy:
            return [], policies

        c = 'customer__radius_credentials__'
        qs = (
            ServiceConnection.objects
            .filter(status='ACTIVE', plan_id__in=list(plan_policy), **{c + 'is_enabled': True})
            .filter(Q(**{c + 'expiration_date__isnull': True}) | Q(**{c + 'expiration_date__gt': now}))
            .values_list('id', 'plan_id', 'customer_id', 'download_speed', 'upload_speed',
                         c + 'username', c + 'subscription_activated_at', c + 'created_at')
        )

        meta, pairs = [], []
        for svc_id, plan_id, cust_id, dl, ul, username, anchor, created in qs.iterator(chunk_size=2000):
            since = anchor or created
            if not username or not since:
                continue
            meta.append((svc_id, plan_id, cust_id, dl, ul, username, since))
            pairs.append((username, since))

        usage = self._usage(pairs)
        limits = {pid: p.limit_bytes for pid, p in policies.items()}

        rows = []
        for svc_id, plan_id, cust_id, dl, ul, username, since in meta:
            pid = plan_policy[plan_id]
            limit = limits[pid]
            used = int(usage.get(username, 0))
            rows.append({
                'svc_id': svc_id, 'plan_id': plan_id, 'customer_id': cust_id,
                'dl': dl or 0, 'ul': ul or 0, 'username': username,
                'policy_id': pid, 'usage': used, 'limit': limit,
                'pct': round(used * 100 / limit, 2) if limit else 0,
            })
        return rows, policies

    # ───────────────────────── run ─────────────────────────
    def run(self):
        rows, policies = self.evaluate()
        states = {
            s.service_connection_id: s
            for s in FUPThrottleState.objects.filter(active=True, service_connection__isnull=False)
        }

        seen = set()
        throttled = released = errors = 0
        n = 0
        for r in rows:
            seen.add(r['svc_id'])
            over = r['usage'] > r['limit']
            state = states.get(r['svc_id'])
            try:
                if over and state is None:
                    policy = policies[r['policy_id']]
                    if policy.auto_enforce:
                        self._throttle(r, policy, delay=n % 60)  # spread router kicks
                        throttled += 1
                        n += 1
                elif not over and state is not None:
                    if self._release(state, 'Usage within limit / period reset'):
                        released += 1
            except Exception:
                errors += 1
                logger.exception('[FUP] %s svc=%s failed', self.schema, r['svc_id'])

        # active throttles whose service/policy/plan link no longer qualifies
        for svc_id in set(states) - seen:
            try:
                if self._release(states[svc_id], 'Policy/plan no longer applies'):
                    released += 1
            except Exception:
                errors += 1
                logger.exception('[FUP] %s orphan release failed svc=%s', self.schema, svc_id)

        cache.set(lastrun_key(self.schema), timezone.now().isoformat(), 86400)
        cache.set(snapshot_key(self.schema), self.build_snapshot(rows, policies), SNAPSHOT_TTL)

        if throttled or released or errors:
            logger.info('[FUP] %s checked=%d throttled=%d released=%d errors=%d',
                        self.schema, len(rows), throttled, released, errors)
        return {'checked': len(rows), 'throttled': throttled, 'released': released, 'errors': errors}

    # ───────────────────────── actions ─────────────────────────
    def _kick(self, username, delay=0):
        def _go():
            from apps.radius.tasks import kick_pppoe_session
            kick_pppoe_session.apply_async(args=[self.schema, username], countdown=delay)
        transaction.on_commit(_go)

    def _throttle(self, r, policy, delay=0):
        from apps.radius.models import RadReply

        now = timezone.now()
        username = r['username']
        rate = _rate(policy.throttle_download_mbps, policy.throttle_upload_mbps)

        with transaction.atomic():
            FUPThrottleState.objects.update_or_create(
                service_connection_id=r['svc_id'],
                defaults=dict(
                    policy=policy, customer_id=r['customer_id'], hotspot_session=None,
                    original_download_mbps=r['dl'], original_upload_mbps=r['ul'],
                    throttled_download_mbps=policy.throttle_download_mbps,
                    throttled_upload_mbps=policy.throttle_upload_mbps,
                    active=True, reason='FUP limit exceeded',
                    released_at=None, last_synced_at=now,   # last_synced_at == "throttled at"
                ),
            )
            FUPViolation.objects.create(
                policy=policy, plan_id=r['plan_id'], service_connection_id=r['svc_id'],
                customer_id=r['customer_id'], total_usage_bytes=r['usage'],
                limit_bytes=r['limit'], exceeded_by_bytes=max(0, r['usage'] - r['limit']),
                action_taken='THROTTLED', status='OPEN',
            )
            updated = RadReply.objects.filter(username=username, attribute=RATE_ATTR).update(op=':=', value=rate)
            if not updated:
                RadReply.objects.create(username=username, attribute=RATE_ATTR, op=':=',
                                        value=rate, customer_id=r['customer_id'])
            self._kick(username, delay)

    def _release(self, state, reason, creds=None):
        from apps.radius.models import CustomerRadiusCredentials

        if creds is None and state.customer_id:
            creds = CustomerRadiusCredentials.objects.filter(customer_id=state.customer_id).first()

        # Renewal already rebuilt RADIUS + kicked the session; only the DB record needs closing.
        renewed = bool(creds and creds.subscription_activated_at and state.last_synced_at
                       and creds.subscription_activated_at > state.last_synced_at)

        state.active = False
        state.reason = reason[:255]
        state.released_at = timezone.now()
        state.save(update_fields=['active', 'reason', 'released_at'])
        if state.service_connection_id:
            FUPViolation.objects.filter(
                service_connection_id=state.service_connection_id, status='OPEN'
            ).update(status='RESOLVED', notes=reason[:250])

        if creds and creds.is_enabled and not renewed:
            creds._is_syncing = True
            try:
                creds.sync_to_radius()          # rebuilds original rate (burst included)
            except Exception:
                logger.exception('[FUP] resync failed for %s; will retry next sweep', creds.username)
                state.active = True
                state.released_at = None
                state.save(update_fields=['active', 'released_at'])
                return False
            finally:
                creds._is_syncing = False
            self._kick(creds.username)
        return True

    def release_policy(self, policy, reason):
        n = 0
        for st in FUPThrottleState.objects.filter(policy=policy, active=True):
            if self._release(st, reason):
                n += 1
        return n

    # ───────────────────────── snapshot (Usage tab) ─────────────────────────
    def build_snapshot(self, rows, policies):
        from apps.billing.models.billing_models import Plan
        from apps.customers.models import Customer

        top = sorted(rows, key=lambda r: r['pct'], reverse=True)[:SNAPSHOT_SIZE]
        if not top:
            return []
        throttled = set(FUPThrottleState.objects.filter(
            active=True, service_connection_id__in=[r['svc_id'] for r in top]
        ).values_list('service_connection_id', flat=True))
        customers = {c.id: c for c in Customer.objects.filter(
            id__in={r['customer_id'] for r in top}).select_related('user')}
        plans = dict(Plan.objects.filter(id__in={r['plan_id'] for r in top}).values_list('id', 'name'))

        out = []
        for r in top:
            cust = customers.get(r['customer_id'])
            code = getattr(cust, 'customer_code', '') or ''
            out.append({
                'id': str(r['svc_id']),
                'customer_name': (getattr(cust, 'full_name', '') or code or r['username']) if cust else r['username'],
                'customer_code': code,
                'policy_name': policies[r['policy_id']].name,
                'plan_name': plans.get(r['plan_id'], ''),
                'total_gb': round(r['usage'] / 1024 ** 3, 2),
                'limit_gb': round(r['limit'] / 1024 ** 3, 2),
                'usage_percent': r['pct'],
                'is_throttled': r['svc_id'] in throttled,
                'status': 'THROTTLED' if r['svc_id'] in throttled else 'NORMAL',
            })
        return out