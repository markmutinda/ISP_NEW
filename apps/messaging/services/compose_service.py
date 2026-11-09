"""
Personalised "Send SMS" from the compose dialog.
Variables look like {first_name}. Unknown {tokens} are left untouched.
Context data is loaded in a few batched queries (no per-recipient queries).
"""
import logging
import re
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger(__name__)

_VAR_RE = re.compile(r'\{(\w+)\}')

MAX_RECIPIENTS = 500
INLINE_LIMIT = 5          # <= this many are sent inside the request, more go to Celery
MAX_MESSAGE_CHARS = 1000


def render_placeholders(text: str, ctx: dict) -> str:
    def _sub(m):
        key = m.group(1)
        if key in ctx:
            val = ctx[key]
            return '' if val is None else str(val)
        return m.group(0)
    return _VAR_RE.sub(_sub, text or '')


def _base_ctx(phone, name, company_name):
    from apps.messaging.services.notification_sender import _get_expiry_context
    parts = (name or '').split(None, 1)
    ctx = {
        'first_name': parts[0] if parts else 'Customer',
        'last_name': parts[1] if len(parts) > 1 else '',
        'full_name': (name or '').strip() or 'Customer',
        'phone': phone,
        'email': '',
        'plan_name': '',
        'amount_due': '',
        'customer_account': '',
        'company_name': company_name or '',
    }
    ctx.update(_get_expiry_context(None))
    return ctx


def _fill_pppoe(ctxs, customer_ids, pppoe):
    from apps.customers.models import Customer, ServiceConnection
    from apps.messaging.services.notification_sender import _get_expiry_context

    ids = list(pppoe)

    # best service per customer: ACTIVE preferred, otherwise the newest
    best = {}
    services = (
        ServiceConnection.objects
        .filter(customer_id__in=ids)
        .exclude(status__in=['TERMINATED', 'CANCELLED'])
        .select_related('plan')
        .order_by('created_at')
    )
    for s in services:
        cur = best.get(s.customer_id)
        if cur is None or (s.status == 'ACTIVE') >= (cur.status == 'ACTIVE'):
            best[s.customer_id] = s

    for c in Customer.objects.filter(id__in=ids).select_related('user', 'radius_credentials'):
        phone = pppoe[c.id]
        ctx = ctxs[phone]
        customer_ids[phone] = c.id

        u = c.user
        if u:
            ctx.update(
                first_name=(u.first_name or '').strip() or 'Customer',
                last_name=(u.last_name or '').strip(),
                full_name=u.get_full_name() or ctx['full_name'],
                email=u.email or '',
            )

        s = best.get(c.id)
        if s:
            ctx['customer_account'] = s.billing_account_number or ''
            if s.plan:
                ctx['plan_name'] = s.plan.name or ''
                ctx['amount_due'] = f"KES {float(s.plan.base_price or 0):,.0f}"

        try:
            expiry = c.radius_credentials.expiration_date
        except Exception:
            expiry = None
        if expiry:
            ctx.update(_get_expiry_context(expiry))


def _fill_hotspot(ctxs, hotspot):
    from apps.billing.models.hotspot_models import HotspotSession
    from apps.messaging.services.notification_sender import _get_expiry_context

    latest = {}
    sessions = (
        HotspotSession.objects
        .filter(hotspot_client_id__in=list(hotspot), status='active', expires_at__gt=timezone.now())
        .select_related('plan')
        .order_by('expires_at')
    )
    for s in sessions:
        latest[s.hotspot_client_id] = s

    for cid, s in latest.items():
        ctx = ctxs[hotspot[cid]]
        if s.plan:
            ctx['plan_name'] = getattr(s.plan, 'name', '') or ''
            try:
                ctx['amount_due'] = f"KES {float(getattr(s.plan, 'price', 0) or 0):,.0f}"
            except Exception:
                pass
        ctx['customer_account'] = getattr(s, 'access_code', '') or ''
        if s.expires_at:
            ctx.update(_get_expiry_context(s.expires_at))


def build_contexts(recipients, company_name=''):
    """Returns (ctx_by_phone, customer_id_by_phone)."""
    ctxs = {r['phone']: _base_ctx(r['phone'], r.get('name'), company_name) for r in recipients}
    customer_ids = {}

    pppoe, hotspot = {}, {}
    for r in recipients:
        rid = str(r.get('id') or '')
        if not rid.isdigit():
            continue
        if r.get('type') == 'pppoe':
            pppoe[int(rid)] = r['phone']
        elif r.get('type') == 'hotspot':
            hotspot[int(rid)] = r['phone']

    # A lookup problem must never block sending — fall back to base values.
    if pppoe:
        try:
            _fill_pppoe(ctxs, customer_ids, pppoe)
        except Exception:
            logger.exception("compose: PPPoE context lookup failed")
    if hotspot:
        try:
            _fill_hotspot(ctxs, hotspot)
        except Exception:
            logger.exception("compose: hotspot context lookup failed")
    return ctxs, customer_ids


def send_personalized(schema_name, recipients, message, company_name='', dispatcher=None):
    """
    Sends one personalised SMS per recipient and records each in SMSMessage.
    The dispatcher is the single place that debits/refunds the wallet.
    """
    from django_tenants.utils import schema_context
    from apps.messaging.models import SMSMessage
    from apps.messaging.services.gateway_dispatcher import GatewayDispatcher

    results = []
    with schema_context(schema_name):
        dispatcher = dispatcher or GatewayDispatcher(schema_name=schema_name)
        provider = dispatcher.config.provider
        msg_type = 'single' if len(recipients) == 1 else 'bulk'
        ctxs, customer_ids = build_contexts(recipients, company_name)

        for r in recipients:
            phone = r['phone']
            text = render_placeholders(message, ctxs.get(phone, {}))
            try:
                res = dispatcher.send_sms(to=phone, message=text)
            except Exception as exc:
                res = {'success': False, 'error': str(exc), 'status': 'failed'}

            ok = bool(res.get('success'))
            err = '' if ok else str(res.get('error') or 'Send failed')[:500]
            try:
                cost = Decimal(str(res.get('cost') or 0))
            except Exception:
                cost = Decimal('0')

            try:
                SMSMessage.objects.create(
                    recipient=phone[:20],
                    recipient_name=(r.get('name') or '')[:120],
                    customer_id=customer_ids.get(phone),
                    message=text,
                    status='sent' if ok else 'failed',
                    type=msg_type,
                    provider=provider,
                    provider_message_id=str(res.get('provider_id') or '')[:100],
                    cost=cost,
                    error_message=err or None,
                    sent_at=timezone.now() if ok else None,
                )
            except Exception:
                logger.exception("compose: could not log SMS to %s***", phone[:6])

            results.append({'phone': phone, 'success': ok, 'error': err})
    return results