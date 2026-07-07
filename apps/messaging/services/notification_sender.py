"""
Unified SMS Notification Sender

Reads SMSNotificationSettings for every toggle, then dispatches
via GatewayDispatcher (inbuilt Bytewave or custom provider).

FIX: All PPPoE notification methods now fetch the saved tenant template
     and pass a comprehensive set of variable aliases so any variable
     a tenant puts in their template will be substituted correctly.
"""

import logging
from decimal import Decimal
from django.core.cache import cache as _cache

logger = logging.getLogger(__name__)


# ── HELPERS ──────────────────────────────────────────────────────────────────

def _get_notif_settings():
    """Fetch SMSNotificationSettings, return None on failure."""
    try:
        from apps.messaging.models import SMSNotificationSettings
        return SMSNotificationSettings.get_settings()
    except Exception:
        return None


def _log_sms(phone: str, message: str, status: str = 'sent',
              msg_type: str = 'automated', recipient_name: str = '',
              customer_id=None, provider_id: str = ''):
    """
    Persist every outbound SMS to SMSMessage so the History tab shows ALL messages.
    Uses a nested savepoint to avoid poisoning parent billing transactions.
    """
    try:
        from apps.messaging.models import SMSMessage
        from django.utils import timezone as tz
        from django.db import transaction

        sent_at = tz.now() if status == 'sent' else None

        safe_recipient = str(phone or '')[:20]
        safe_status = str(status or 'sent')[:20]
        safe_type = str(msg_type or 'automated')[:20]
        safe_recipient_name = str(recipient_name or '')[:120]
        safe_provider_id = str(provider_id or '')[:100]

        with transaction.atomic():
            SMSMessage.objects.create(
                recipient=safe_recipient,
                recipient_name=safe_recipient_name,
                message=message,
                status=safe_status,
                type=safe_type,
                provider='system',
                provider_message_id=safe_provider_id,
                sent_at=sent_at,
                customer_id=customer_id if customer_id else None,
            )
        logger.debug(f"SMS logged cleanly: {safe_status} to {safe_recipient[:6]}***")
    except Exception as exc:
        logger.warning("[SMS Log] could not persist message: %s", exc)


def _render(template: str, **ctx) -> str:
    """Substitute {key} placeholders. Unknown keys are left as-is."""
    for key, value in ctx.items():
        template = template.replace('{' + key + '}', str(value or ''))
    return template


def _get_rendered_message(event_type: str, default_msg: str, **context) -> str:
    """
    Fetch the active custom template for event_type from the DB.
    Prefers tenant-customised rows (is_system=False) over system defaults.
    Substitutes all {variable} placeholders with context values.
    Falls back to default_msg if no template found or template is blank.
    """
    from apps.messaging.models import SMSTemplate

    # Prefer tenant-customised template, fall back to system default
    template = (
        SMSTemplate.objects
        .filter(event_type=event_type, is_active=True)
        .order_by('is_system')  # False (0) before True (1) — tenant custom first
        .first()
    )

    if not template or not template.content.strip():
        rendered = default_msg
    else:
        rendered = template.content

    # Substitute all {key} placeholders
    for key, value in context.items():
        rendered = rendered.replace('{' + key + '}', str(value) if value is not None else '')

    return rendered


def _get_settings():
    from apps.messaging.models import SMSNotificationSettings
    return SMSNotificationSettings.get_settings()


def _dispatch(phone: str, message: str, schema_name: str = None) -> bool:
    """Send via active gateway."""
    if not phone:
        return False
    try:
        from apps.messaging.services.gateway_dispatcher import GatewayDispatcher
        from django.db import connection

        _schema = schema_name or getattr(connection, 'schema_name', None)
        dispatcher = GatewayDispatcher(schema_name=_schema)
        result = dispatcher.send_sms(to=phone, message=message)

        if result.get("success"):
            logger.info(f"SMS sent to {phone[:6]}***")
            return True
        logger.warning(f"SMS failed to {phone[:6]}***: {result.get('error')}")
        return False
    except ValueError as e:
        logger.warning(f"SMS not sent (no gateway): {e}")
        return False
    except Exception as e:
        logger.error(f"SMS dispatch error: {e}", exc_info=True)
        return False


def _send_once(dedup_key: str, phone: str, message: str, ttl: int = 600,
               schema_name: str = None) -> bool:
    """
    Send SMS exactly once within the TTL window for the given dedup_key.
    Uses DB dedup (INSERT ... ON CONFLICT) as primary guard,
    cache as secondary fast-path.
    """
    from django.db import IntegrityError, transaction as _tx
    from apps.messaging.models import SMSDeduplicationLog

    full_key = f"sms_once:{dedup_key}"

    # Fast path: cache check
    if _cache.get(full_key):
        logger.debug(f"SMS deduped via cache (key={full_key})")
        return False

    # DB-level dedup: atomic insert — if row exists, skip
    try:
        with _tx.atomic():
            SMSDeduplicationLog.objects.create(dedup_key=full_key)
    except IntegrityError:
        logger.info(f"SMS deduped via DB (key={full_key})")
        return False
    except Exception as e:
        # If dedup model doesn't exist yet (pre-migration), fall through
        logger.warning(f"SMS dedup DB check failed (non-fatal): {e}")

    # Set cache so subsequent calls in same process skip DB hit
    _cache.set(full_key, 1, ttl)

    result = _dispatch(phone, message, schema_name=schema_name)

    if result:
        _log_sms(phone, message, status='sent', msg_type='automated')
    else:
        _log_sms(phone, message, status='failed', msg_type='automated')

    return result


def _fmt_phone(phone: str) -> str:
    """Normalize to standard 254XXXXXXXXX format."""
    if not phone:
        return ""

    raw = str(phone).strip()
    if raw.startswith('+'):
        raw = raw[1:]

    digits = "".join(c for c in raw if c.isdigit())

    if len(digits) < 9 or len(digits) > 12:
        return ""

    if digits.startswith("00"):
        digits = digits[2:]

    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif (digits.startswith("7") or digits.startswith("1")) and len(digits) == 9:
        digits = "254" + digits
    elif digits.startswith("254") and len(digits) == 12:
        pass
    else:
        return ""

    if len(digits) != 12 or not digits.startswith("254"):
        return ""

    return digits


def _customer_phone(customer, service=None) -> str:
    """Extract best available phone from a Customer instance."""
    try:
        if customer.user and getattr(customer.user, 'phone_number', None):
            val = str(customer.user.phone_number).strip()
            if len(val) <= 15:
                return val
    except Exception:
        pass

    try:
        alt = customer.alternative_phone or ""
        if len(alt) <= 15:
            return alt
    except Exception:
        pass

    try:
        if service and hasattr(service, 'phone_number') and service.phone_number:
            val = str(service.phone_number).strip()
            if len(val) <= 15:
                return val
    except Exception:
        pass

    return ""


def _get_customer_context(customer) -> dict:
    """
    Build a comprehensive context dict for a customer so ALL reasonable
    variable names a tenant might use in their template are populated.
    """
    name = ""
    first_name = ""
    last_name = ""
    full_name = ""
    phone = ""

    try:
        first_name = customer.user.first_name or ""
        last_name = customer.user.last_name or ""
        full_name = customer.user.get_full_name() or ""
        name = first_name or full_name or "Customer"
        phone = getattr(customer.user, 'phone_number', '') or ""
    except Exception:
        name = "Customer"

    # Billing account number
    customer_account = ""
    try:
        service = customer.services.filter(status='ACTIVE').first()
        if service and service.billing_account_number:
            customer_account = service.billing_account_number
        elif hasattr(customer, 'billing_account_number') and customer.billing_account_number:
            customer_account = customer.billing_account_number
    except Exception:
        pass

    return {
        # Name aliases
        'name': name,
        'customer_name': name,
        'first_name': first_name,
        'last_name': last_name,
        'full_name': full_name,
        # Account aliases
        'customer_account': customer_account,
        'account': customer_account,
        'account_number': customer_account,
        'billing_account': customer_account,
        'paybill_account': customer_account,
        # Phone
        'phone': phone,
        'phone_number': phone,
    }


def _get_plan_context(service=None, customer=None) -> dict:
    """Build plan-related context variables."""
    plan_name = ""
    amount = ""
    amount_due = ""

    try:
        if service and service.plan:
            plan_name = service.plan.name or ""
            price = float(service.plan.base_price or 0)
            amount = f"{price:,.0f}"
            amount_due = f"KES {price:,.0f}"
        elif customer:
            svc = customer.services.filter(status='ACTIVE', plan__isnull=False).first()
            if svc and svc.plan:
                plan_name = svc.plan.name or ""
                price = float(svc.plan.base_price or 0)
                amount = f"{price:,.0f}"
                amount_due = f"KES {price:,.0f}"
    except Exception:
        pass

    return {
        'plan_name': plan_name,
        'plan': plan_name,
        'amount': amount,
        'amount_due': amount_due,
        'renewal_amount': amount,
        'subscription_amount': amount,
    }


def _get_expiry_context(expiration_date=None) -> dict:
    """
    Build all expiry-related context variables from a datetime.
    
    FIX: Uses calendar date comparison (local timezone) instead of hour delta
    to decide "today" vs "tomorrow" vs a full date. This prevents incorrect
    "today at 09:59" messages when expiry is actually tomorrow.
    """
    if not expiration_date:
        return {
            'expiry_date': 'N/A',
            'expiry_time': 'N/A',
            'expiry_full': 'N/A',
            'expiry_display': 'soon',
            'new_expiry': 'N/A',
            'expires_at': 'N/A',
            'expiration': 'N/A',
            'expiry': 'N/A',
            'valid_until': 'N/A',
        }

    from django.utils import timezone as _tz
    local_tz = _tz.get_current_timezone()
    local_expiry = expiration_date.astimezone(local_tz)

    expiry_date_str = local_expiry.strftime('%d %b %Y')
    expiry_time_str = local_expiry.strftime('%H:%M')
    expiry_full_str = local_expiry.strftime('%d %b %Y at %H:%M')

    now = _tz.now()
    now_local = now.astimezone(local_tz)
    hours_left = (expiration_date - now).total_seconds() / 3600
    
    expiry_day = local_expiry.date()
    today = now_local.date()
    days_diff = (expiry_day - today).days

    # ============================================================
    # FIX: Use calendar date comparison for "today" vs "tomorrow"
    # This prevents "today at 09:59" when expiry is actually tomorrow
    # ============================================================
    if hours_left <= 1:
        expiry_display = f"in less than 1 hour (at {expiry_time_str})"
    elif days_diff <= 0:
        expiry_display = f"today at {expiry_time_str}"
    elif days_diff == 1:
        expiry_display = f"tomorrow at {expiry_time_str}"
    else:
        expiry_display = f"on {expiry_date_str} at {expiry_time_str}"

    return {
        'expiry_date': expiry_date_str,
        'expiry_time': expiry_time_str,
        'expiry_full': expiry_full_str,
        'expiry_display': expiry_display,
        # Common aliases tenants might use
        'new_expiry': expiry_display,          # "today at 20:59" or "on 15 Jan 2026 at 20:59"
        'expires_at': expiry_display,
        'expiration': expiry_full_str,
        'expiry': expiry_full_str,
        'valid_until': expiry_full_str,
        'expiry_date_only': expiry_date_str,
        'expiry_time_only': expiry_time_str,
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN NOTIFIER CLASS
# ─────────────────────────────────────────────────────────────────────────────

class SMSNotifier:
    """
    All automated SMS go through this class.
    Each method:
      1. Checks its toggle
      2. Fetches the saved template for that event type
      3. Builds a comprehensive context with ALL variable aliases
      4. Renders and sends
    """

    # ─────────────────────────────────────────────────────────────────
    # PPPOE / STATIC
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def pppoe_welcome(customer, username: str = "", password: str = "",
                      schema_name: str = None) -> bool:
        """Called when a new PPPoE/Static service is activated."""
        s = _get_notif_settings()
        if s and not s.pppoe_welcome:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False

        ctx = _get_customer_context(customer)

        default_msg = (
            f"Hi {ctx['name']}, welcome aboard! Your account number is {ctx['customer_account']}."
        )
        msg = _get_rendered_message('pppoe_welcome', default_msg, **ctx)
        return _send_once(f"pppoe_welcome:{customer.id}", phone, msg, ttl=3600,
                          schema_name=schema_name)

    # REMOVED: pppoe_new_subscription (no longer used)

    @staticmethod
    def pppoe_payment(customer, amount: float, reference: str = "",
                      service=None, schema_name: str = None) -> bool:
        """Called after a PPPoE payment is completed."""
        s = _get_notif_settings()
        if s and not s.pppoe_payment_confirmation:
            return False
        phone = _fmt_phone(_customer_phone(customer, service=service))
        if not phone:
            return False

        ctx = _get_customer_context(customer)
        ctx.update(_get_plan_context(service=service, customer=customer))

        # Expiry from credentials
        try:
            creds = customer.radius_credentials
            ctx.update(_get_expiry_context(creds.expiration_date))
        except Exception:
            ctx.update(_get_expiry_context(None))

        amount_fmt = f"{float(amount):,.0f}"
        ctx['amount'] = amount_fmt
        ctx['amount_paid'] = amount_fmt
        ctx['reference'] = reference or ''
        ctx['payment_ref'] = reference or ''
        ctx['receipt'] = reference or ''

        default_msg = (
            f"Hi {ctx['name']}, payment of KES {amount_fmt} received. "
            f"Reference: {reference}. Thank you!"
        )
        msg = _get_rendered_message('pppoe_payment', default_msg, **ctx)
        return _send_once(f"pppoe_pay:{customer.id}:{reference}", phone, msg, ttl=3600,
                          schema_name=schema_name)

    @staticmethod
    def pppoe_renewal(customer, plan_name: str, expires_at=None, reference: str = "",
                      schema_name: str = None) -> bool:
        """
        Called after successful renewal - uses the merged payment/renewal toggle.
        (FIXED: now checks pppoe_payment_confirmation instead of removed pppoe_renewal_confirmation)
        """
        s = _get_notif_settings()
        # FIX: Use merged payment_confirmation toggle
        if s and not s.pppoe_payment_confirmation:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False

        ctx = _get_customer_context(customer)
        ctx.update(_get_expiry_context(expires_at))
        ctx['plan_name'] = plan_name
        ctx['plan'] = plan_name
        ctx['reference'] = reference or ''
        ctx['receipt'] = reference or ''
        ctx['mpesa_receipt'] = reference or ''

        # Amount from plan
        try:
            svc = customer.services.filter(status='ACTIVE', plan__isnull=False).first()
            if svc and svc.plan:
                price = float(svc.plan.base_price or 0)
                ctx['amount'] = f"{price:,.0f}"
                ctx['amount_due'] = f"KES {price:,.0f}"
        except Exception:
            pass

        default_msg = (
            f"Hi {ctx['name']}, payment of KES {ctx.get('amount','')} received for {plan_name}. "
            f"Your subscription is now valid until {ctx['expiry_full']}. Thank you!"
        )
        msg = _get_rendered_message('pppoe_payment', default_msg, **ctx)
        from django.utils import timezone as _tz
        ts = int(_tz.now().timestamp() // 3600)
        return _send_once(f"pppoe_renew:{customer.id}:{ts}", phone, msg, ttl=3600,
                          schema_name=schema_name)

    @staticmethod
    def pppoe_expiry_reminder(customer, days_left: int, plan_name: str = "",
                               schema_name: str = None) -> bool:
        """
        Called X days/hours before PPPoE expiry.
        Uses the saved 'pppoe_expiry_reminder' template with ALL variable aliases.
        """
        s = _get_notif_settings()
        if s and not s.pppoe_expiry_reminder:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False

        ctx = _get_customer_context(customer)

        # Fetch expiry date from RADIUS credentials
        expiration_date = None
        try:
            creds = customer.radius_credentials
            expiration_date = creds.expiration_date
        except Exception:
            pass

        ctx.update(_get_expiry_context(expiration_date))

        # Plan context
        amount_due_str = ""
        try:
            svc = customer.services.filter(status='ACTIVE', plan__isnull=False).first()
            if svc and svc.plan:
                if not plan_name:
                    plan_name = svc.plan.name or ""
                price = float(svc.plan.base_price or 0)
                amount_due_str = f"KES {price:,.0f}"
                ctx['amount'] = f"{price:,.0f}"
                ctx['amount_due'] = amount_due_str
                ctx['renewal_amount'] = f"{price:,.0f}"
                ctx['price'] = f"{price:,.0f}"
        except Exception:
            pass

        ctx['plan_name'] = plan_name
        ctx['plan'] = plan_name
        ctx['days_left'] = days_left
        ctx['days'] = days_left

        default_msg = (
            f"Hi {ctx['name']}, your {plan_name} plan expires {ctx['expiry_display']}. "
            f"Renew now{' - ' + amount_due_str if amount_due_str else ''} to avoid disconnection."
        )
        msg = _get_rendered_message('pppoe_expiry_reminder', default_msg, **ctx)

        # Plain dispatch (dedup handled at task level via DB)
        result = _dispatch(phone, msg, schema_name=schema_name)
        if result:
            _log_sms(phone, msg, status='sent', msg_type='automated',
                     recipient_name=ctx['name'], customer_id=customer.id)
        else:
            _log_sms(phone, msg, status='failed', msg_type='automated',
                     recipient_name=ctx['name'], customer_id=customer.id)
        return result

    @staticmethod
    def pppoe_expired_notice(customer, plan_name: str = "", schema_name: str = None) -> bool:
        """
        Called once when a customer's subscription has actually expired
        (not a reminder — the period is over).
        """
        s = _get_notif_settings()
        if s and not s.pppoe_expiry_notification:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False

        ctx = _get_customer_context(customer)

        if not plan_name:
            try:
                svc = customer.services.filter(plan__isnull=False).order_by('-updated_at').first()
                if svc and svc.plan:
                    plan_name = svc.plan.name or ""
            except Exception:
                pass

        try:
            svc = customer.services.filter(plan__isnull=False).order_by('-updated_at').first()
            if svc and svc.plan:
                price = float(svc.plan.base_price or 0)
                ctx['amount'] = f"{price:,.0f}"
                ctx['amount_due'] = f"KES {price:,.0f}"
        except Exception:
            pass

        ctx['plan_name'] = plan_name
        ctx['plan'] = plan_name

        default_msg = (
            f"Hi {ctx['name']}, your internet subscription ({plan_name}) has expired. "
            f"Please renew to restore your connection."
        )
        msg = _get_rendered_message('pppoe_expiry_notification', default_msg, **ctx)

        # Build a dedup key that includes the expiry date to allow a new notice after renewal
        expiry_timestamp = None
        try:
            creds = customer.radius_credentials
            if creds and creds.expiration_date:
                expiry_timestamp = int(creds.expiration_date.timestamp())
        except Exception:
            pass

        if expiry_timestamp:
            dedup_key = f"pppoe_expired:{customer.id}:{expiry_timestamp}"
        else:
            dedup_key = f"pppoe_expired:{customer.id}"

        return _send_once(dedup_key, phone, msg, ttl=3600, schema_name=schema_name)

    # REMOVED: pppoe_suspended, pppoe_resumed, pppoe_plan_changed (no longer used)

    @staticmethod
    def pppoe_invoice_issued(customer, invoice, schema_name: str = None) -> bool:
        """Called when an invoice is issued."""
        s = _get_notif_settings()
        if s and not s.pppoe_payment_confirmation:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False

        ctx = _get_customer_context(customer)

        due_date_str = invoice.due_date.strftime('%d %b %Y') if invoice.due_date else 'N/A'
        amount_fmt = f"{float(invoice.total_amount):,.0f}"
        invoice_no = invoice.invoice_number or str(invoice.id)

        ctx['amount'] = amount_fmt
        ctx['amount_due'] = f"KES {amount_fmt}"
        ctx['invoice_number'] = invoice_no
        ctx['invoice_no'] = invoice_no
        ctx['due_date'] = due_date_str

        default_msg = (
            f"Hi {ctx['name']}, invoice #{invoice_no} of KES {amount_fmt} "
            f"is due on {due_date_str}. Pay to avoid disconnection."
        )
        msg = _get_rendered_message('pppoe_invoice_issued', default_msg, **ctx)
        return _send_once(f"invoice:{invoice.id}", phone, msg, ttl=3600,
                          schema_name=schema_name)

    # ─────────────────────────────────────────────────────────────────
    # HOTSPOT
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def hotspot_welcome(session, schema_name: str = None) -> bool:
        """Welcome SMS when a hotspot session activates."""
        s = _get_notif_settings()
        if s and not s.hotspot_welcome:
            return False
        phone = _fmt_phone(session.phone_number)
        if not phone:
            return False

        plan = session.plan

        # Build expiry string
        expiry_time = 'N/A'
        if session.expires_at:
            from django.utils import timezone as _tz
            local_tz = _tz.get_current_timezone()
            expiry_time = session.expires_at.astimezone(local_tz).strftime('%d %b %Y %H:%M')

        duration = plan.duration_display if plan and hasattr(plan, 'duration_display') else ''
        speed = plan.speed_display if plan and hasattr(plan, 'speed_display') else (
            f"{plan.speed_limit_mbps} Mbps" if plan else ''
        )
        plan_name = plan.name if plan else ''
        access_code = session.access_code or ''

        default_msg = (
            f"WiFi Active! Code: {access_code}. "
            f"Plan: {plan_name} ({duration}). "
            f"Expires: {expiry_time}. Speed: {speed}. Enjoy!"
        )
        msg = _get_rendered_message(
            'hotspot_welcome',
            default_msg,
            access_code=access_code,
            plan_name=plan_name,
            duration=duration,
            expiry_time=expiry_time,
            speed=speed,
        )
        return _send_once(
            f"hs_welcome:{session.session_id}",
            phone, msg, ttl=3600,
            schema_name=schema_name,
        )

    @staticmethod
    def hotspot_session_expired(session, schema_name: str = None) -> bool:
        """Notify when hotspot session has fully expired."""
        s = _get_notif_settings()
        if s and not s.hotspot_session_expired:
            return False
        phone = _fmt_phone(session.phone_number)
        if not phone:
            return False

        plan_name = session.plan.name if session.plan else ''

        default_msg = (
            f"Your WiFi session has ended. "
            f"Visit the portal to buy a new plan and reconnect. Thank you!"
        )
        msg = _get_rendered_message(
            'hotspot_session_expired',
            default_msg,
            plan_name=plan_name,
        )
        return _send_once(
            f"hs_expired:{session.session_id}",
            phone, msg, ttl=3600,
            schema_name=schema_name,
        )

    # REMOVED METHODS:
    # - hotspot_new_subscription
    # - hotspot_expiry_warning
    # - hotspot_payment_failed

    # ─────────────────────────────────────────────────────────────────
    # VOUCHERS
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def hotspot_voucher_sold(voucher, schema_name: str = None) -> bool:
        try:
            customer = voucher.sold_to
            if not customer:
                return False
            phone = _fmt_phone(_customer_phone(customer))
            if not phone:
                return False
            plan = voucher.batch.hotspot_plan if (
                voucher.batch and hasattr(voucher.batch, 'hotspot_plan')
            ) else None
            default_msg = (
                f"Voucher: {voucher.code} | PIN: {voucher.pin or ''} | "
                f"Plan: {plan.name if plan else 'Hotspot'} | "
                f"Value: KES {float(voucher.face_value):,.0f}. Enjoy!"
            )
            msg = _get_rendered_message(
                'hotspot_voucher_sold', default_msg,
                code=voucher.code,
                pin=voucher.pin or '',
                plan_name=plan.name if plan else 'Hotspot',
                face_value=f"{float(voucher.face_value):,.0f}",
            )
            return _send_once(f"voucher_sold:{voucher.id}", phone, msg, ttl=3600,
                              schema_name=schema_name)
        except Exception as exc:
            logger.warning("hotspot_voucher_sold SMS failed: %s", exc)
            return False

    @staticmethod
    def voucher_sold(voucher, schema_name: str = None) -> bool:
        try:
            customer = voucher.sold_to
            if not customer:
                return False
            phone = _fmt_phone(_customer_phone(customer))
            if not phone:
                return False
            default_msg = (
                f"Your voucher is ready! Code: {voucher.code} | PIN: {voucher.pin or ''} | "
                f"Value: KES {float(voucher.face_value):,.0f}."
            )
            msg = _get_rendered_message(
                'voucher_sold', default_msg,
                code=voucher.code,
                pin=voucher.pin or '',
                face_value=f"{float(voucher.face_value):,.0f}",
            )
            return _send_once(f"voucher:{voucher.id}", phone, msg, ttl=3600,
                              schema_name=schema_name)
        except Exception as exc:
            logger.warning("voucher_sold SMS failed: %s", exc)
            return False