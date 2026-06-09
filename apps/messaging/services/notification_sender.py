"""
Unified SMS Notification Sender

Reads SMSNotificationSettings for every toggle, then dispatches
via GatewayDispatcher (inbuilt Bytewave or custom provider).

Usage:
    from apps.messaging.services.notification_sender import SMSNotifier
    SMSNotifier.hotspot_welcome(session)
    SMSNotifier.pppoe_payment(customer, amount, reference)
"""

import logging
from decimal import Decimal
from django.core.cache import cache as _cache

logger = logging.getLogger(__name__)


# ── ADDED HELPER FUNCTIONS ──────────────────────────────────────────────

def _get_notif_settings():
    """Fetch SMSNotificationSettings, return None on failure."""
    try:
        from apps.messaging.models import SMSNotificationSettings
        return SMSNotificationSettings.get_settings()
    except Exception:
        return None


# ============================================================
# HARDENED _log_sms - prevents transaction poisoning
# ============================================================
def _log_sms(phone: str, message: str, status: str = 'sent',
              msg_type: str = 'automated', recipient_name: str = '',
              customer_id=None, provider_id: str = ''):
    """
    Persist every outbound SMS to SMSMessage so the History tab shows ALL messages.
    
    HARDENING FIX: Uses an explicit nested transaction.atomic savepoint block and string 
    truncation to prevent VARCHAR length errors from poisoning parent billing transactions.
    """
    try:
        from apps.messaging.models import SMSMessage
        from django.utils import timezone as tz
        from django.db import transaction
        
        # Ensure we have a valid datetime for sent_at
        sent_at = tz.now() if status == 'sent' else None
        
        # ✂️ Force hard truncation to prevent VARCHAR constraint violations on disk
        safe_recipient = str(phone or '')[:20]
        safe_status = str(status or 'sent')[:20]
        safe_type = str(msg_type or 'automated')[:20]
        safe_recipient_name = str(recipient_name or '')[:120]
        safe_provider_id = str(provider_id or '')[:100]

        # 🛡️ Use a nested atomic checkpoint to completely isolate this query from the parent billing block
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
        logger.warning("[SMS Log] could not persist message (isolated via savepoint safely): %s", exc)


def _render(template: str, **ctx) -> str:
    """
    Substitute {key} placeholders in *template* with values from *ctx*.
    Unknown keys are left as-is so nothing breaks.
    """
    for key, value in ctx.items():
        template = template.replace('{' + key + '}', str(value or ''))
    return template


def _get_rendered_message(event_type: str, default_msg: str, **context) -> str:
    """
    Fetches the active custom template for a given event from the database.
    Replaces {variable} placeholders with actual values.
    Falls back to the hardcoded default_msg if no template exists.
    """
    from apps.messaging.models import SMSTemplate
    
    # Query the database for the user's saved template for this specific event
    template = SMSTemplate.objects.filter(event_type=event_type, is_active=True).first()
    
    if not template or not template.content.strip():
        return default_msg
        
    msg = template.content
    # Dynamically inject the context variables (e.g., {name}, {amount}) into the text
    for key, value in context.items():
        msg = msg.replace(f"{{{key}}}", str(value))
        
    return msg


def _get_settings():
    from apps.messaging.models import SMSNotificationSettings
    return SMSNotificationSettings.get_settings()


# ─────────────────────────────────────────────────────────────────────────────
# FIXED: _dispatch now accepts schema_name parameter for Celery task safety
# ─────────────────────────────────────────────────────────────────────────────

def _dispatch(phone: str, message: str, schema_name: str = None) -> bool:
    """
    Send via active gateway. 
    
    Args:
        phone: Recipient phone number
        message: SMS content
        schema_name: Explicit tenant schema name (REQUIRED for Celery tasks)
    
    Returns:
        bool: True on success, False otherwise
    """
    if not phone:
        logger.debug("SMS skipped: no phone number")
        return False
    
    try:
        from apps.messaging.services.gateway_dispatcher import GatewayDispatcher
        from django.db import connection

        # Determine schema: explicit > current connection > None
        _schema = schema_name or getattr(connection, 'schema_name', None)
        
        # CRITICAL: Pass schema_name explicitly to avoid cross-tenant leaks
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
    Logs every attempt to SMSMessage.
    
    Args:
        dedup_key: Unique key for deduplication
        phone: Recipient phone number
        message: SMS content
        ttl: Time-to-live in seconds for dedup cache
        schema_name: Explicit tenant schema name (for Celery tasks)
    
    Returns:
        bool: True if sent, False if deduped or failed
    """
    full_key = f"sms_once:{dedup_key}"
    if _cache.get(full_key):
        logger.debug(f"SMS deduped (key={full_key})")
        return False
    
    # Pass schema_name to _dispatch
    result = _dispatch(phone, message, schema_name=schema_name)
    
    if result:
        _cache.set(full_key, 1, ttl)
        # Log successful send
        _log_sms(phone, message, status='sent', msg_type='automated')
    else:
        # Log failed send
        _log_sms(phone, message, status='failed', msg_type='automated')
    
    return result


def _fmt_phone(phone: str) -> str:
    """
    Normalize strings to standard 2547xxxxxxxx or 2541xxxxxxxx Kenyan mobile formats.
    Rejects internal database auto-generated identifiers or corrupt digits safely.
    """
    if not phone:
        return ""
        
    # Extract only the raw numbers
    digits = "".join(c for c in phone if c.isdigit())
    
    # Strip any leading international zeros if a user typed something like 000254...
    if digits.startswith("00"):
        digits = digits[2:]
        
    # Standardize local 07... or 01... inputs to international 254... codes
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    elif (digits.startswith("7") or digits.startswith("1")) and len(digits) == 9:
        digits = "254" + digits
        
    # Strip accidental prepended '+' characters that escaped the digit match
    if not digits.startswith("254"):
        if digits.startswith("7") or digits.startswith("1"):
            digits = "254" + digits
        else:
            digits = "254" + digits.lstrip("+")
            
    # 🚨 HARDENED SECURITY CHECK: A true Kenyan international number must be exactly 12 digits long
    # (e.g., 254 + 9 mobile numbers). If it's shorter or longer, it's an internal test string or corruption.
    if len(digits) != 12:
        logger.warning(f"[PHONE SANITIZER] Aborted layout for invalid number reference string: '{phone}' (Cleaned: {digits})")
        return ""
        
    return digits


class SMSNotifier:
    """
    All automated SMS go through this class.
    Each method checks its toggle before dispatching.
    """

    # ─────────────────────────────────────────────────────────────────
    # HOTSPOT
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def hotspot_new_subscription(session, schema_name: str = None) -> bool:
        """Called right after STK push succeeds (payment initiated)."""
        s = _get_notif_settings()
        if s and not s.hotspot_new_subscription:
            return False
        phone = _fmt_phone(session.phone_number)
        if not phone:
            return False
        plan = session.plan
        default_msg = (
            f"Hi! Your {plan.name if plan else ''} plan purchase is confirmed. "
            f"Access code: {session.access_code or ''}. Enjoy your browsing!"
        )
        msg = _get_rendered_message(
            event_type='hotspot_new_subscription',
            default_msg=default_msg,
            plan_name=plan.name if plan else '',
            access_code=session.access_code or '',
        )
        return _send_once(f"hs_new:{session.session_id}", phone, msg, ttl=3600, schema_name=schema_name)

    @staticmethod
    def hotspot_welcome(session, schema_name: str = None) -> bool:
        """Called after session activates — sends access code & expiry."""
        s = _get_notif_settings()
        if s and not s.hotspot_welcome:
            return False
        phone = _fmt_phone(session.phone_number)
        if not phone:
            return False
        plan = session.plan
        expires = ""
        expiry_time = "N/A"
        if session.expires_at:
            from django.utils import timezone
            local_tz = timezone.get_current_timezone()
            expires = session.expires_at.astimezone(local_tz).strftime("%H:%M")
            expires = f" Expires at {expires}"
            expiry_time = session.expires_at.astimezone(local_tz).strftime("%d %b %Y %H:%M")
        default_msg = (
            f"WiFi Active! Code: {session.access_code or ''}. Plan: {plan.name if plan else ''} ({plan.duration_display if hasattr(plan, 'duration_display') else ''}){expires}. "
            f"Speed: {plan.speed_display if hasattr(plan, 'speed_display') else ''}. Enjoy!"
        )
        msg = _get_rendered_message(
            event_type='hotspot_welcome',
            default_msg=default_msg,
            access_code=session.access_code or '',
            plan_name=plan.name if plan else '',
            duration=plan.duration_display if hasattr(plan, 'duration_display') else '',
            expires=expires or '',
            expiry_time=expiry_time,
            speed=plan.speed_display if hasattr(plan, 'speed_display') else '',
        )
        return _send_once(f"hs_welcome:{session.session_id}", phone, msg, ttl=3600, schema_name=schema_name)

    @staticmethod
    def hotspot_expiry_warning(session, schema_name: str = None) -> bool:
        """Called by Celery task X minutes before expiry."""
        s = _get_notif_settings()
        if s and not s.hotspot_session_expiry:
            return False
        phone = _fmt_phone(session.phone_number)
        if not phone:
            return False
        from django.utils import timezone
        mins = max(0, int((session.expires_at - timezone.now()).total_seconds() / 60)) \
            if session.expires_at else 0
        default_msg = (
            f"Your {session.plan.name if session.plan else ''} hotspot session expires in {mins} minutes. "
            f"Renew to stay connected! Code: {session.access_code or ''}"
        )
        msg = _get_rendered_message(
            event_type='hotspot_expiry_warning',
            default_msg=default_msg,
            plan_name=session.plan.name if session.plan else '',
            minutes_left=mins,
            access_code=session.access_code or '',
        )
        return _send_once(f"hotspot_expiry:{session.session_id}", phone, msg, ttl=600, schema_name=schema_name)

    @staticmethod
    def hotspot_session_expired(session, schema_name: str = None) -> bool:
        """Called when session is marked expired."""
        s = _get_notif_settings()
        if s and not s.hotspot_session_expired:
            return False
        phone = _fmt_phone(session.phone_number)
        if not phone:
            return False
        default_msg = f"Your {session.plan.name if session.plan else ''} session has expired. Buy a new plan to reconnect!"
        msg = _get_rendered_message(
            event_type='hotspot_session_expired',
            default_msg=default_msg,
            plan_name=session.plan.name if session.plan else '',
        )
        return _send_once(f"hs_expired:{session.session_id}", phone, msg, ttl=3600, schema_name=schema_name)

    @staticmethod
    def hotspot_payment_failed(session, reason: str = "", schema_name: str = None) -> bool:
        """Called when STK push fails or is cancelled."""
        s = _get_notif_settings()
        if s and not s.hotspot_payment_failed:
            return False
        phone = _fmt_phone(session.phone_number)
        if not phone:
            return False
        default_msg = f"Payment for {session.plan.name if session.plan else ''} failed. {reason} Please try again."
        msg = _get_rendered_message(
            event_type='hotspot_payment_failed',
            default_msg=default_msg,
            plan_name=session.plan.name if session.plan else '',
            reason=reason or '',
        )
        return _send_once(f"hs_payfail:{session.session_id}", phone, msg, ttl=3600, schema_name=schema_name)

    # ─────────────────────────────────────────────────────────────────
    # PPPOE / STATIC
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def pppoe_welcome(customer, username: str = "", password: str = "", schema_name: str = None) -> bool:
        """Called when a new PPPoE/Static service is activated."""
        s = _get_notif_settings()
        if s and not s.pppoe_welcome:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False
        name = customer.user.first_name or "Customer"
        default_msg = (
            f"Hi {name}, your internet is ready! "
            f"Username: {username} | Password: {password} | Welcome aboard!"
        )
        msg = _get_rendered_message(
            event_type='pppoe_welcome',
            default_msg=default_msg,
            customer_name=name,
            name=name,
            username=username,
            password=password,
        )
        return _send_once(f"pppoe_welcome:{customer.id}", phone, msg, ttl=3600, schema_name=schema_name)

    @staticmethod
    def pppoe_new_subscription(customer, plan_name: str, amount: float,
                                expires_at=None, schema_name: str = None) -> bool:
        """Called when admin sets up a new subscription."""
        s = _get_notif_settings()
        if s and not s.pppoe_new_subscription:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False
        name = customer.user.first_name or "Customer"
        expiry_str = expires_at.strftime('%d %b %Y') if expires_at else 'N/A'
        
        # Try to fetch credentials
        try:
            creds = customer.radius_credentials
            username = creds.username or ''
            password = creds.password or ''
        except Exception:
            username = password = ''
        
        default_msg = (
            f"Hi {name}, welcome! Plan: {plan_name} | "
            f"Username: {username} | Password: {password} | "
            f"Expires: {expiry_str}. KES {float(amount):,.0f} paid."
        )
        msg = _get_rendered_message(
            event_type='pppoe_new_subscription',
            default_msg=default_msg,
            customer_name=name,
            name=name,
            plan_name=plan_name,
            username=username,
            password=password,
            expires_at=expiry_str,
            expiry_date=expiry_str,
            amount=f"{float(amount):,.0f}",
        )
        return _send_once(f"pppoe_new:{customer.id}", phone, msg, ttl=3600, schema_name=schema_name)

    @staticmethod
    def pppoe_payment(customer, amount: float, reference: str = "", schema_name: str = None) -> bool:
        """Called after a PPPoE payment is completed."""
        s = _get_notif_settings()
        if s and not s.pppoe_payment_confirmation:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False
        name = customer.user.first_name or "Customer"
        
        # Fetch plan name for context
        plan_name = ''
        try:
            service = customer.services.filter(status='ACTIVE', plan__isnull=False).first()
            if service and service.plan:
                plan_name = service.plan.name or ''
        except Exception:
            pass
        
        default_msg = (
            f"Hi {name}, payment of KES {float(amount):,.0f} received. "
            f"Reference: {reference}. Thank you!"
        )
        msg = _get_rendered_message(
            event_type='pppoe_payment',
            default_msg=default_msg,
            customer_name=name,
            name=name,
            amount=f"{float(amount):,.0f}",
            reference=reference or '',
            plan_name=plan_name,
        )
        return _send_once(f"pppoe_pay:{customer.id}:{reference}", phone, msg, ttl=3600, schema_name=schema_name)

    @staticmethod
    def pppoe_renewal(customer, plan_name: str, expires_at=None, schema_name: str = None) -> bool:
        """Called after successful renewal."""
        s = _get_notif_settings()
        if s and not s.pppoe_renewal_confirmation:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False
        name = customer.user.first_name or "Customer"
        expiry_str = expires_at.strftime('%d %b %Y') if expires_at else 'N/A'
        default_msg = (
            f"Hi {name}, subscription renewed! Plan: {plan_name} | "
            f"Expires: {expiry_str}. Thank you!"
        )
        msg = _get_rendered_message(
            event_type='pppoe_renewal',
            default_msg=default_msg,
            customer_name=name,
            name=name,
            plan_name=plan_name,
            expires_at=expiry_str,
            expiry_date=expiry_str,
        )
        return _send_once(f"pppoe_renew:{customer.id}:{expiry_str}", phone, msg, ttl=3600, schema_name=schema_name)

    @staticmethod
    def pppoe_expiry_reminder(customer, days_left: int, plan_name: str = "", schema_name: str = None) -> bool:
        """
        Called X days before PPPoE expiry.
        
        FIX: Enhanced with smart expiry display (1 hour, 6 hours, today, etc.)
        FIX: Added {customer_account} variable for billing account number.
        """
        s = _get_notif_settings()
        if s and not s.pppoe_expiry_reminder:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False
        name = customer.user.first_name or "Customer"

        # Fetch expiry info
        try:
            creds = customer.radius_credentials
            expiration_date = creds.expiration_date
            service = customer.services.filter(status='ACTIVE', plan__isnull=False).first()
            amount_due = f"KES {float(service.plan.base_price):,.0f}" if service and service.plan else ''
        except Exception:
            expiration_date = None
            amount_due = ''

        # Get customer's billing account number for {customer_account} variable
        customer_account = ''
        try:
            service = customer.services.filter(status='ACTIVE').first()
            if service and service.billing_account_number:
                customer_account = service.billing_account_number
            elif hasattr(customer, 'billing_account_number') and customer.billing_account_number:
                customer_account = customer.billing_account_number
        except Exception:
            pass

        # Build smart expiry display
        if expiration_date:
            from django.utils import timezone as _tz
            local_tz = _tz.get_current_timezone()
            local_expiry = expiration_date.astimezone(local_tz)
            expiry_time_str = local_expiry.strftime('%H:%M')
            expiry_date_str = local_expiry.strftime('%d %b %Y')
            expiry_full_str = local_expiry.strftime('%d %b %Y at %H:%M')

            now = _tz.now()
            hours_left = (expiration_date - now).total_seconds() / 3600

            if hours_left <= 1:
                expiry_display = f"in less than 1 hour (at {expiry_time_str})"
            elif hours_left <= 6:
                expiry_display = f"in {int(hours_left)} hour(s) at {expiry_time_str}"
            elif hours_left <= 24:
                expiry_display = f"today at {expiry_time_str}"
            else:
                expiry_display = f"on {expiry_date_str} at {expiry_time_str}"
        else:
            expiry_date_str = 'N/A'
            expiry_full_str = 'N/A'
            expiry_time_str = 'N/A'
            expiry_display = 'soon'
            amount_due = amount_due

        default_msg = (
            f"Hi {name}, your {plan_name} plan expires {expiry_display}. "
            f"Renew now{ ' - ' + amount_due if amount_due else ''} to avoid disconnection."
        )
        msg = _get_rendered_message(
            event_type='pppoe_expiry_reminder',
            default_msg=default_msg,
            customer_name=name,
            name=name,
            plan_name=plan_name,
            days_left=days_left,
            days=days_left,
            expiry_date=expiry_date_str,
            expiry_time=expiry_time_str,
            expiry_display=expiry_display,
            expiry_full=expiry_full_str,
            amount_due=amount_due,
            amount=amount_due,
            customer_account=customer_account,
        )

        # Plain dispatch with schema — dedup is handled at task level via DB
        result = _dispatch(phone, msg, schema_name=schema_name)
        if result:
            _log_sms(phone, msg, status='sent', msg_type='automated',
                     recipient_name=name, customer_id=customer.id)
        else:
            _log_sms(phone, msg, status='failed', msg_type='automated',
                     recipient_name=name, customer_id=customer.id)
        return result

    @staticmethod
    def pppoe_suspended(customer, reason: str = "", schema_name: str = None) -> bool:
        """Called when service is suspended."""
        s = _get_notif_settings()
        if s and not s.pppoe_service_suspended:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False
        name = customer.user.first_name or "Customer"
        default_msg = (
            f"Hi {name}, your internet service has been suspended. "
            f"Reason: {reason or 'subscription expired'}. Contact support to restore."
        )
        msg = _get_rendered_message(
            event_type='pppoe_suspended',
            default_msg=default_msg,
            customer_name=name,
            name=name,
            reason=reason or 'subscription expired',
        )
        return _send_once(f"pppoe_suspend:{customer.id}", phone, msg, ttl=3600, schema_name=schema_name)

    @staticmethod
    def pppoe_resumed(customer, schema_name: str = None) -> bool:
        """Called when service is restored."""
        s = _get_notif_settings()
        if s and not s.pppoe_service_resumed:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False
        name = customer.user.first_name or "Customer"
        
        service = customer.services.filter(status='ACTIVE').first()
        plan_name = service.plan.name if (service and service.plan) else ''
        
        default_msg = (
            f"Hi {name}, your internet service has been restored! "
            f"Plan: {plan_name}. Welcome back."
        )
        msg = _get_rendered_message(
            event_type='pppoe_resumed',
            default_msg=default_msg,
            customer_name=name,
            name=name,
            plan_name=plan_name,
        )
        return _send_once(f"pppoe_resume:{customer.id}", phone, msg, ttl=3600, schema_name=schema_name)

    @staticmethod
    def pppoe_plan_changed(customer, old_plan: str, new_plan: str, schema_name: str = None) -> bool:
        """Called when a plan is changed."""
        s = _get_notif_settings()
        if s and not s.pppoe_plan_changed:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False
        name = customer.user.first_name or "Customer"
        default_msg = (
            f"Hi {name}, your plan has been changed from {old_plan} "
            f"to {new_plan}. Enjoy!"
        )
        msg = _get_rendered_message(
            event_type='pppoe_plan_changed',
            default_msg=default_msg,
            customer_name=name,
            name=name,
            old_plan=old_plan,
            new_plan=new_plan,
        )
        return _send_once(f"pppoe_plan:{customer.id}:{new_plan}", phone, msg, ttl=3600, schema_name=schema_name)

    @staticmethod
    def pppoe_invoice_issued(customer, invoice, schema_name: str = None) -> bool:
        """Called when an invoice is issued."""
        s = _get_notif_settings()
        if s and not s.pppoe_payment_confirmation:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False
        name = customer.user.first_name or "Customer"
        default_msg = (
            f"Hi {name}, invoice #{invoice.invoice_number or str(invoice.id)} of KES {float(invoice.total_amount):,.0f} "
            f"is due on {invoice.due_date.strftime('%d %b %Y') if invoice.due_date else 'N/A'}. Pay to avoid disconnection."
        )
        msg = _get_rendered_message(
            event_type='pppoe_invoice_issued',
            default_msg=default_msg,
            customer_name=name,
            name=name,
            invoice_number=invoice.invoice_number or str(invoice.id),
            amount=f"{float(invoice.total_amount):,.0f}",
            due_date=invoice.due_date.strftime('%d %b %Y') if invoice.due_date else 'N/A',
        )
        return _send_once(f"invoice:{invoice.id}", phone, msg, ttl=3600, schema_name=schema_name)

    @staticmethod
    def hotspot_voucher_sold(voucher, schema_name: str = None) -> bool:
        """Called when a hotspot voucher is sold."""
        try:
            customer = voucher.sold_to
            if not customer:
                return False
            phone = _fmt_phone(_customer_phone(customer))
            if not phone:
                return False
            
            plan = voucher.batch.hotspot_plan if (voucher.batch and hasattr(voucher.batch, 'hotspot_plan')) else None
            default_msg = (
                f"Voucher: {voucher.code} | PIN: {voucher.pin or ''} | Plan: {plan.name if plan else 'Hotspot'} | "
                f"Value: KES {float(voucher.face_value):,.0f}. Enjoy your internet!"
            )
            msg = _get_rendered_message(
                event_type='hotspot_voucher_sold',
                default_msg=default_msg,
                code=voucher.code,
                pin=voucher.pin or '',
                plan_name=plan.name if plan else 'Hotspot',
                face_value=f"{float(voucher.face_value):,.0f}",
            )
            return _send_once(f"voucher_sold:{voucher.id}", phone, msg, ttl=3600, schema_name=schema_name)
        except Exception as exc:
            logger.warning("hotspot_voucher_sold SMS failed: %s", exc)
            return False

    @staticmethod
    def voucher_sold(voucher, schema_name: str = None) -> bool:
        """Generic voucher (non-hotspot)."""
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
                event_type='voucher_sold',
                default_msg=default_msg,
                code=voucher.code,
                pin=voucher.pin or '',
                face_value=f"{float(voucher.face_value):,.0f}",
            )
            return _send_once(f"voucher:{voucher.id}", phone, msg, ttl=3600, schema_name=schema_name)
        except Exception as exc:
            logger.warning("voucher_sold SMS failed: %s", exc)
            return False


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _customer_phone(customer) -> str:
    """Extract best available phone from a Customer instance."""
    # 1. Primary path: user.phone_number (the one we confirmed works)
    try:
        if customer.user and getattr(customer.user, 'phone_number', None):
            val = str(customer.user.phone_number)
            # If it's a valid number, return it. If it's a long hex hash (>20 chars), ignore it.
            if len(val) < 20 and any(c.isdigit() for c in val):
                return val
    except Exception:
        pass
    
    # 2. Fallback path
    try:
        return customer.alternative_phone or ""
    except Exception:
        return ""