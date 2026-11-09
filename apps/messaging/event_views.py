import re

from django.db import IntegrityError, connection, transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import HasRoleAccessPolicy, IsAdminOrStaff
from .models import SMSTemplate
from .services.compose_service import (
    INLINE_LIMIT, MAX_MESSAGE_CHARS, MAX_RECIPIENTS, send_personalized,
)
from .services.gateway_dispatcher import GatewayDispatcher
from .template_defaults import DEFAULT_BY_EVENT, EVENT_SLOTS

MAX_TEMPLATE_CHARS = 1000
EVENT_KEYS = [e[0] for e in EVENT_SLOTS]
EVENT_META = {e[0]: (e[1], e[2]) for e in EVENT_SLOTS}
_VAR_RE = re.compile(r'\{(\w+)\}')


# ─────────────────────────────────────────────────────────────────────────────
# ALLOWED VARIABLES — must match what each sender in notification_sender.py
# actually fills. Anything outside this list is a typo and is rejected on save.
# ─────────────────────────────────────────────────────────────────────────────

_CUSTOMER_VARS = {
    'name', 'customer_name', 'first_name', 'last_name', 'full_name',
    'customer_account', 'account', 'account_number', 'billing_account',
    'paybill_account', 'phone', 'phone_number',
}
_PLAN_VARS = {
    'plan_name', 'plan', 'amount', 'amount_due', 'renewal_amount', 'subscription_amount',
}
_EXPIRY_VARS = {
    'expiry_date', 'expiry_time', 'expiry_full', 'expiry_display', 'new_expiry',
    'expires_at', 'expiration', 'expiry', 'valid_until', 'expiry_date_only', 'expiry_time_only',
}

# Exactly what each sender in notification_sender.py supplies for that event.
ALLOWED_VARS = {
    'hotspot_welcome': {
        'access_code', 'username', 'password', 'credentials', 'device_count',
        'plan_name', 'duration', 'expiry_time', 'speed', 'amount', 'amount_due', 'amount_paid',
    },
    'pppoe_welcome': set(_CUSTOMER_VARS),
    'pppoe_payment': (_CUSTOMER_VARS | _PLAN_VARS | _EXPIRY_VARS
                      | {'amount_paid', 'reference', 'payment_ref', 'receipt', 'mpesa_receipt'}),
    'pppoe_expiry_reminder': (_CUSTOMER_VARS | _PLAN_VARS | _EXPIRY_VARS
                              | {'days_left', 'days', 'price'}),
    'pppoe_expiry_notification': _CUSTOMER_VARS | _PLAN_VARS,
}

# Variables the Send SMS compose dialog fills per recipient (compose_service.py)
COMPOSE_ALLOWED_VARS = {
    'first_name', 'last_name', 'full_name', 'phone', 'email', 'plan_name',
    'amount_due', 'customer_account', 'company_name',
    'expiry_date', 'expiry_time', 'expiry_full', 'expiry_display',
    'new_expiry', 'expires_at', 'expiration', 'expiry', 'valid_until',
}


def _unknown_vars(text, allowed):
    return sorted(set(_VAR_RE.findall(text or '')) - allowed)


def _load_event_rows(events):
    """
    Exactly ONE active row per event type (DB also enforces this with a partial
    unique constraint). 1 query when everything exists; creates / reactivates
    only what is missing.
    """
    rows, spare = {}, {}
    qs = (SMSTemplate.objects.filter(event_type__in=events)
          .order_by('is_system', '-updated_at', '-id'))      # same pick-order as the senders
    for t in qs:
        (rows if t.is_active else spare).setdefault(t.event_type, t)

    for ev in events:
        if ev in rows:
            continue
        try:
            with transaction.atomic():
                t = spare.get(ev)
                if t:
                    t.is_active = True
                    t.save(update_fields=['is_active', 'updated_at'])
                else:
                    name, content, variables = DEFAULT_BY_EVENT[ev]
                    t = SMSTemplate.objects.create(
                        event_type=ev, is_system=True, name=name,
                        content=content, variables=variables, is_active=True,
                    )
        except IntegrityError:  # concurrent request created it first
            t = (SMSTemplate.objects.filter(event_type=ev, is_active=True)
                 .order_by('is_system', '-updated_at', '-id').first())
        if t:
            rows[ev] = t
    return rows


def _serialize(ev, tpl):
    label, description = EVENT_META[ev]
    default = DEFAULT_BY_EVENT[ev][1]
    return {
        'event_type': ev,
        'label': label,
        'description': description,
        'content': tpl.content,
        'default_content': default,
        'is_customized': tpl.content.strip() != default.strip(),
    }


class SMSEventTemplatesView(APIView):
    """
    GET  /messaging/templates/events/                -> the 5 automation templates
    PUT  /messaging/templates/events/<event_type>/   -> save (update-in-place)
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/sms"

    def get(self, request, event_type=None):
        rows = _load_event_rows(EVENT_KEYS)
        return Response([_serialize(ev, rows[ev]) for ev in EVENT_KEYS if ev in rows])

    def put(self, request, event_type=None):
        if event_type not in EVENT_META:
            return Response({'detail': 'Unknown template event.'}, status=status.HTTP_404_NOT_FOUND)

        content = request.data.get('content')
        if not isinstance(content, str) or not content.strip():
            return Response({'detail': 'Message cannot be empty.'}, status=status.HTTP_400_BAD_REQUEST)
        content = content.strip()
        if len(content) > MAX_TEMPLATE_CHARS:
            return Response({'detail': f'Message is too long (max {MAX_TEMPLATE_CHARS} characters).'},
                            status=status.HTTP_400_BAD_REQUEST)

        unknown = _unknown_vars(content, ALLOWED_VARS[event_type])
        if unknown:
            return Response(
                {'detail': 'Unknown variable(s): ' + ', '.join('{%s}' % u for u in unknown)
                           + '. Use the variable buttons below the message box.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tpl = _load_event_rows([event_type]).get(event_type)
        if not tpl:
            return Response({'detail': 'Could not load template. Please retry.'},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)

        tpl.content = content
        tpl.variables = ['{%s}' % v for v in sorted(set(_VAR_RE.findall(content)))]
        tpl.save(update_fields=['content', 'variables', 'updated_at'])
        return Response(_serialize(event_type, tpl))


class SMSComposeView(APIView):
    """
    POST /messaging/sms/compose/
    { "message": "Hi {first_name}...", "recipients": [{"phone","name","type","id"}] }
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/sms"

    def post(self, request):
        message = request.data.get('message')
        raw = request.data.get('recipients')

        if not isinstance(message, str) or not message.strip():
            return Response({'detail': 'Message is required.'}, status=status.HTTP_400_BAD_REQUEST)
        message = message.strip()
        if len(message) > MAX_MESSAGE_CHARS:
            return Response({'detail': f'Message is too long (max {MAX_MESSAGE_CHARS} characters).'},
                            status=status.HTTP_400_BAD_REQUEST)

        unknown = _unknown_vars(message, COMPOSE_ALLOWED_VARS)
        if unknown:
            return Response(
                {'detail': 'Unknown variable(s): ' + ', '.join('{%s}' % u for u in unknown)
                           + '. Use the variable buttons below the message box.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not isinstance(raw, list) or not raw:
            return Response({'detail': 'Add at least one recipient.'}, status=status.HTTP_400_BAD_REQUEST)

        recipients, seen = [], set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            phone = str(item.get('phone') or '').strip()
            if not phone or phone in seen:
                continue
            seen.add(phone)
            recipients.append({
                'phone': phone[:20],
                'name': str(item.get('name') or '')[:120],
                'type': item.get('type') if item.get('type') in ('pppoe', 'hotspot') else '',
                'id': str(item.get('id') or '')[:20],
            })
        if not recipients:
            return Response({'detail': 'Add at least one valid recipient.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(recipients) > MAX_RECIPIENTS:
            return Response({'detail': f'Too many recipients (max {MAX_RECIPIENTS}). Use Campaigns for larger sends.'},
                            status=status.HTTP_400_BAD_REQUEST)

        schema = getattr(connection, 'schema_name', None)
        if not schema or schema == 'public':
            return Response({'detail': 'Cannot send from the public portal.'}, status=status.HTTP_400_BAD_REQUEST)

        try:  # fail fast if no gateway is configured
            dispatcher = GatewayDispatcher(schema_name=schema)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        company = getattr(request, 'company', None)
        company_name = getattr(company, 'name', '') or getattr(request.user, 'company_name', '') or ''

        if len(recipients) <= INLINE_LIMIT:
            results = send_personalized(schema, recipients, message, company_name, dispatcher=dispatcher)
            sent = sum(1 for r in results if r['success'])
            return Response({'queued': False, 'sent': sent, 'failed': len(results) - sent,
                             'results': results})

        try:
            from .tasks import send_personalized_sms
            send_personalized_sms.delay(schema, recipients, message, company_name)
        except Exception:
            return Response({'detail': 'Could not queue the messages. Please try again.'},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'queued': True, 'count': len(recipients)}, status=status.HTTP_202_ACCEPTED)