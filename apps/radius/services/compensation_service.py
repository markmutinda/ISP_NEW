"""
Bulk "compensation": add time to active PPPoE / hotspot subscriptions.

Design:
  * Audience resolved server-side from filters (never ship thousands of ids).
  * Applied in chunks with bulk_update (no per-row save() => no signal storm,
    no mass session kick).
  * Idempotent: targets are fixed at resolve time; a re-run skips rows already
    at their target, so a Celery redelivery cannot double-credit.
"""
import logging
import re
import time
import uuid
from datetime import timedelta, timezone as dt_tz

from django.core.cache import cache
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

JOB_TTL = 6 * 3600
LOCK_TTL = 2 * 3600
STALE_AFTER = 600            # a "running" job with no heartbeat for 10 min is considered dead
CHUNK = 400
SMS_CHUNK = 100
SMS_STAGGER_SECONDS = 3
MAX_DELTA = timedelta(days=30)
EXP_FMT = "%b %d %Y %H:%M:%S"          # same format RadiusSyncService writes (UTC)
PPPOE_TIMEOUT_CAP = 30 * 24 * 3600     # same cap as CustomerRadiusCredentials.sync_to_radius
UNITS = {'MINUTES': 'minutes', 'HOURS': 'hours', 'DAYS': 'days'}


def job_key(schema, job_id):
    return f"comp:job:{schema}:{job_id}"


def targets_key(schema, job_id):
    return f"comp:targets:{schema}:{job_id}"


def lock_key(schema):
    return f"comp:lock:{schema}"


def new_job_id():
    return uuid.uuid4().hex


def parse_delta(amount, unit):
    unit = str(unit or '').upper()
    if unit not in UNITS:
        raise ValueError("Duration unit must be MINUTES, HOURS or DAYS.")
    try:
        n = int(amount)
    except (TypeError, ValueError):
        raise ValueError("Duration must be a whole number.")
    if n <= 0:
        raise ValueError("Duration must be greater than zero.")
    delta = timedelta(**{UNITS[unit]: n})
    if delta > MAX_DELTA:
        raise ValueError("Maximum compensation per run is 30 days.")
    return delta


# ───────────────────────── audience ─────────────────────────

def _phone_fragment(search):
    digits = re.sub(r'\D', '', search or '')
    norm = re.sub(r'^(254|0)', '', digits)
    return norm if len(norm) >= 3 else ''


def pppoe_queryset(now, router_id=None, search=''):
    from apps.radius.models import CustomerRadiusCredentials
    qs = CustomerRadiusCredentials.objects.filter(is_enabled=True, expiration_date__gt=now)
    if router_id:
        qs = qs.filter(router_id=router_id)
    if search:
        q = (Q(username__icontains=search)
             | Q(customer__customer_code__icontains=search)
             | Q(customer__user__first_name__icontains=search)
             | Q(customer__user__last_name__icontains=search))
        frag = _phone_fragment(search)
        if frag:
            q |= Q(customer__user__phone_number__icontains=frag)
        qs = qs.filter(q)
    return qs


def hotspot_queryset(now, router_id=None, search=''):
    from apps.billing.models.hotspot_models import HotspotSession
    qs = (HotspotSession.objects
          .filter(status='active', expires_at__gt=now)
          .exclude(access_code__isnull=True).exclude(access_code=''))
    if router_id:
        qs = qs.filter(router_id=router_id)
    if search:
        q = Q(access_code__icontains=search) | Q(mac_address__icontains=search)
        frag = _phone_fragment(search)
        if frag:
            q |= Q(phone_number__icontains=frag) | Q(hotspot_client__canonical_phone__icontains=frag)
        qs = qs.filter(q)
    return qs


def _clean_phone(ph):
    ph = (ph or '').strip()
    if not ph or ph.upper().startswith('MAC-'):
        return ''
    digits = re.sub(r'\D', '', ph)
    return ph if 9 <= len(digits) <= 13 else ''


def build_preview(kind, router_id, search, limit=40):
    now = timezone.now()
    counts = {'pppoe': 0, 'hotspot': 0}
    sample = []

    if kind in ('all', 'pppoe'):
        qs = pppoe_queryset(now, router_id, search)
        counts['pppoe'] = qs.count()
        for r in qs.order_by('username').values(
            'id', 'username', 'expiration_date', 'router__name',
            'customer__user__first_name', 'customer__user__last_name',
            'customer__user__phone_number',
        )[:limit]:
            sample.append({
                'key': f"p:{r['id']}", 'kind': 'pppoe',
                'name': f"{r['customer__user__first_name'] or ''} {r['customer__user__last_name'] or ''}".strip() or r['username'],
                'username': r['username'],
                'phone': r['customer__user__phone_number'] or '',
                'router': r['router__name'] or '',
                'expires_at': r['expiration_date'].isoformat() if r['expiration_date'] else None,
            })

    if kind in ('all', 'hotspot'):
        qs = hotspot_queryset(now, router_id, search)
        counts['hotspot'] = qs.count()
        for r in qs.order_by('access_code').values(
            'id', 'access_code', 'expires_at', 'router__name',
            'phone_number', 'hotspot_client__canonical_phone',
        )[:limit]:
            sample.append({
                'key': f"h:{r['id']}", 'kind': 'hotspot',
                'name': r['access_code'], 'username': r['access_code'],
                'phone': _clean_phone(r['hotspot_client__canonical_phone']) or _clean_phone(r['phone_number']),
                'router': r['router__name'] or '',
                'expires_at': r['expires_at'].isoformat() if r['expires_at'] else None,
            })

    counts['total'] = counts['pppoe'] + counts['hotspot']
    return {'counts': counts, 'sample': sample, 'sample_limit': limit}


def resolve_targets(params, delta, now):
    """Fix each row's old/target expiry up-front (this is what makes re-runs idempotent)."""
    kind = params['type']
    excl = set(params.get('exclude') or [])
    out = {'p': [], 'h': []}

    if kind in ('all', 'pppoe'):
        qs = pppoe_queryset(now, params.get('router_id'), params.get('search')).order_by('id').values_list(
            'id', 'username', 'expiration_date', 'customer_id',
            'customer__user__phone_number', 'customer__user__first_name', 'customer__user__last_name',
        )
        for id_, uname, exp, cid, ph, fn, ln in qs.iterator(chunk_size=2000):
            if f"p:{id_}" in excl:
                continue
            out['p'].append({
                'id': id_, 'u': uname, 'old': exp.isoformat(), 'new': (exp + delta).isoformat(),
                'ph': _clean_phone(ph), 'nm': f"{fn or ''} {ln or ''}".strip() or uname,
                'cid': str(cid or ''),
            })

    if kind in ('all', 'hotspot'):
        qs = hotspot_queryset(now, params.get('router_id'), params.get('search')).order_by('id').values_list(
            'id', 'access_code', 'expires_at', 'hotspot_client_id',
            'hotspot_client__canonical_phone', 'phone_number',
        )
        for id_, code, exp, client_id, cph, sph in qs.iterator(chunk_size=2000):
            if f"h:{id_}" in excl:
                continue
            out['h'].append({
                'id': str(id_), 'u': code, 'old': exp.isoformat(), 'new': (exp + delta).isoformat(),
                'ph': _clean_phone(cph) or _clean_phone(sph), 'nm': code,
                'cid': str(client_id or ''),
            })
    return out


# ───────────────────────── apply ─────────────────────────

def _fmt_exp(dt):
    return dt.astimezone(dt_tz.utc).strftime(EXP_FMT)


def _sync_radius_rows(new_exp_by_user, now, cap=None):
    """Bulk-update radcheck Expiration + radreply Session-Timeout for these usernames."""
    from apps.radius.models import RadCheck, RadReply

    users = list(new_exp_by_user)
    if not users:
        return

    checks = list(RadCheck.objects.filter(attribute='Expiration', username__in=users))
    seen = set()
    for row in checks:
        row.value = _fmt_exp(new_exp_by_user[row.username])
        seen.add(row.username)
    if checks:
        RadCheck.objects.bulk_update(checks, ['value'], batch_size=250)
    missing = [RadCheck(username=u, attribute='Expiration', op=':=', value=_fmt_exp(e))
               for u, e in new_exp_by_user.items() if u not in seen]
    if missing:
        RadCheck.objects.bulk_create(missing)

    replies = list(RadReply.objects.filter(attribute='Session-Timeout', username__in=users))
    for row in replies:
        rem = int((new_exp_by_user[row.username] - now).total_seconds())
        if cap:
            rem = min(rem, cap)
        row.value = str(max(rem, 1))
    if replies:
        RadReply.objects.bulk_update(replies, ['value'], batch_size=250)


def _apply_pppoe(rows, delta, now):
    from apps.radius.models import CustomerRadiusCredentials
    from apps.billing.models.subscription_models import Subscription

    by_id = {r['id']: r for r in rows}
    ok_ids, to_update, customer_ids, new_exp = set(), [], set(), {}

    with transaction.atomic():
        creds = CustomerRadiusCredentials.objects.select_for_update().filter(
            pk__in=list(by_id), is_enabled=True,
        )
        for c in creds:
            cur = c.expiration_date
            if cur is None or cur <= now:
                continue                      # unlimited / expired meanwhile: never resurrect
            if cur == parse_datetime(by_id[c.pk]['new']):
                ok_ids.add(c.pk)              # already applied by an earlier attempt
                continue
            c.expiration_date = cur + delta
            c.updated_at = now
            to_update.append(c)
            ok_ids.add(c.pk)
            customer_ids.add(c.customer_id)
            new_exp[c.username] = c.expiration_date

        if to_update:
            CustomerRadiusCredentials.objects.bulk_update(to_update, ['expiration_date', 'updated_at'], batch_size=250)
            _sync_radius_rows(new_exp, now, cap=PPPOE_TIMEOUT_CAP)
            Subscription.objects.filter(
                customer_id__in=customer_ids, status='ACTIVE', expires_at__isnull=False,
            ).update(expires_at=F('expires_at') + delta, updated_at=now)

    return len(ok_ids), set(by_id) - ok_ids


def _apply_hotspot(rows, delta, now):
    from apps.billing.models.hotspot_models import HotspotSession

    by_id = {r['id']: r for r in rows}
    ok_ids, to_update, new_exp = set(), [], {}

    with transaction.atomic():
        sessions = HotspotSession.objects.select_for_update().filter(pk__in=list(by_id), status='active')
        for s in sessions:
            cur = s.expires_at
            sid = str(s.pk)
            if cur is None or cur <= now:
                continue
            if cur == parse_datetime(by_id[sid]['new']):
                ok_ids.add(sid)
                continue
            s.expires_at = cur + delta
            s.updated_at = now
            to_update.append(s)
            ok_ids.add(sid)
            if s.access_code:
                prev = new_exp.get(s.access_code)
                new_exp[s.access_code] = max(prev, s.expires_at) if prev else s.expires_at

        if to_update:
            HotspotSession.objects.bulk_update(to_update, ['expires_at', 'updated_at'], batch_size=250)
            _sync_radius_rows(new_exp, now)

    return len(ok_ids), set(by_id) - ok_ids


# ───────────────────────── job runner ─────────────────────────

def _save(schema, job_id, job, **kw):
    job.update(kw)
    job['updated'] = time.time()
    cache.set(job_key(schema, job_id), job, JOB_TTL)


def _chunks(targets):
    out = []
    for k in ('p', 'h'):
        rows = targets[k]
        out += [(k, rows[i:i + CHUNK]) for i in range(0, len(rows), CHUNK)]
    return out


def run_job(schema, job_id, params):
    from apps.messaging.tasks import send_personalized_sms

    job = cache.get(job_key(schema, job_id)) or {
        'job_id': job_id, 'cursor': 0, 'failed_chunks': [], 'skip': [],
        'processed': 0, 'compensated': 0, 'skipped': 0, 'failed': 0, 'total': 0,
        'sms_recipients': 0, 'sms_queued': 0, 'sms_dispatched': False,
    }
    try:
        delta = parse_delta(params['amount'], params['unit'])
        _save(schema, job_id, job, state='running')

        targets = cache.get(targets_key(schema, job_id))
        if targets is None:
            if job['cursor']:
                raise RuntimeError("Job state expired mid-run; not resuming to avoid double credit.")
            targets = resolve_targets(params, delta, timezone.now())
            cache.set(targets_key(schema, job_id), targets, JOB_TTL)

        chunks = _chunks(targets)
        _save(schema, job_id, job, total=sum(len(r) for _, r in chunks))

        for idx in range(job['cursor'], len(chunks)):
            kind, rows = chunks[idx]
            try:
                fn = _apply_pppoe if kind == 'p' else _apply_hotspot
                ok, skipped = fn(rows, delta, timezone.now())
                job['compensated'] += ok
                job['skipped'] += len(skipped)
                job['skip'] += [f"{kind}:{i}" for i in skipped]
            except Exception:
                logger.exception("[COMPENSATE] chunk %s failed (schema=%s job=%s)", idx, schema, job_id)
                job['failed'] += len(rows)
                job['failed_chunks'].append(idx)
            job['processed'] += len(rows)
            _save(schema, job_id, job, cursor=idx + 1)

        # ── SMS (after DB is committed so {expiry_*} variables show the NEW expiry) ──
        if params.get('send_sms') and not job['sms_dispatched']:
            bad_chunks, skip = set(job['failed_chunks']), set(job['skip'])
            recipients, seen = [], set()
            for idx, (kind, rows) in enumerate(chunks):
                if idx in bad_chunks:
                    continue
                for r in rows:
                    if not r['ph'] or f"{kind}:{r['id']}" in skip:
                        continue
                    pk = re.sub(r'\D', '', r['ph'])[-9:]
                    if pk in seen:
                        continue
                    seen.add(pk)
                    recipients.append({
                        'phone': r['ph'][:20], 'name': r['nm'][:120],
                        'type': 'pppoe' if kind == 'p' else 'hotspot',
                        'id': r['cid'] if r['cid'].isdigit() else '',
                    })
            queued = 0
            for i in range(0, len(recipients), SMS_CHUNK):
                send_personalized_sms.apply_async(
                    args=[schema, recipients[i:i + SMS_CHUNK], params['message'], params.get('company_name', '')],
                    countdown=(i // SMS_CHUNK) * SMS_STAGGER_SECONDS,
                )
                queued += 1
            _save(schema, job_id, job, sms_dispatched=True, sms_recipients=len(recipients), sms_queued=queued)

        _save(schema, job_id, job, state='done')
    except Exception as e:
        logger.exception("[COMPENSATE] job %s failed (schema=%s)", job_id, schema)
        _save(schema, job_id, job, state='failed', error=str(e)[:300])
    finally:
        if cache.get(lock_key(schema)) == job_id:
            cache.delete(lock_key(schema))
        cache.delete(targets_key(schema, job_id)) if job.get('state') in ('done', 'failed') else None