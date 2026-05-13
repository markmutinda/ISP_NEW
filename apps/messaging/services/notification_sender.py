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


def _log_sms(phone: str, message: str, status: str = 'sent',
              msg_type: str = 'automated', recipient_name: str = '',
              customer_id=None, provider_id: str = ''):
    """
    Persist every outbound SMS to SMSMessage so the History tab shows ALL messages
    (manual, automated, campaign).  Never raises – logging must not break sends.
    """
    try:
        from apps.messaging.models import SMSMessage
        from django.utils import timezone as tz
        SMSMessage.objects.create(
            recipient=phone,
            recipient_name=recipient_name or '',
            message=message,
            status=status,
            type=msg_type,           # frontend reads 'type'
            message_type=msg_type,   # keep both
            provider='system',
            provider_message_id=provider_id or '',
            sent_at=tz.now(),
        )
    except Exception as exc:
        logger.warning("[SMS Log] could not persist message: %s", exc)


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


def _dispatch(phone: str, message: str) -> bool:
    """Send via active gateway. Returns True on success, False otherwise."""
    if not phone:
        logger.debug("SMS skipped: no phone number")
        return False
    try:
        from apps.messaging.services.gateway_dispatcher import GatewayDispatcher
        from apps.messaging.models import SMSNotificationSettings

        dispatcher = GatewayDispatcher()

        # Determine if inbuilt system is active from EITHER the gateway config
        # OR the notification settings (they should be in sync, but check both
        # to handle the case where they drift apart).
        notif_settings = SMSNotificationSettings.get_settings()
        is_inbuilt = dispatcher.use_inbuilt or notif_settings.use_inbuilt_system

        if is_inbuilt:
            from apps.messaging.services.credit_billing_service import CreditBillingService
            from django.db import transaction
            try:
                with transaction.atomic():
                    CreditBillingService.debit_for_sms(
                        message_text=message,
                        # No sms_message object yet — ledger entry links to None
                    )
            except Exception as e:
                logger.warning(f"Automated SMS skipped — insufficient credits: {e}")
                return False

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


def _send_once(dedup_key: str, phone: str, message: str, ttl: int = 600) -> bool:
    """
    Send SMS exactly once within the TTL window for the given dedup_key.
    Logs every attempt to SMSMessage.
    """
    full_key = f"sms_once:{dedup_key}"
    if _cache.get(full_key):
        logger.debug(f"SMS deduped (key={full_key})")
        return False
    
    result = _dispatch(phone, message)
    
    if result:
        _cache.set(full_key, 1, ttl)
        # Log successful send
        _log_sms(phone, message, status='sent', msg_type='automated')
    else:
        # Log failed send
        _log_sms(phone, message, status='failed', msg_type='automated')
    
    return result


def _fmt_phone(phone: str) -> str:
    """Normalize to 2547xxxxxxxx format."""
    if not phone:
        return ""
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    elif digits.startswith("7") or digits.startswith("1"):
        digits = "254" + digits
    if not digits.startswith("254"):
        digits = "254" + digits.lstrip("+")
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
    def hotspot_new_subscription(session) -> bool:
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
        result = _send_once(f"hs_new:{session.session_id}", phone, msg, ttl=3600)
        _log_sms(phone, msg, msg_type='automated', recipient_name=session.phone_number)
        return result

    @staticmethod
    def hotspot_welcome(session) -> bool:
        """Called after session activates — sends access code & expiry."""
        s = _get_notif_settings()
        if s and not s.hotspot_welcome:
            return False
        phone = _fmt_phone(session.phone_number)
        if not phone:
            return False
        plan = session.plan
        expires = ""
        if session.expires_at:
            from django.utils import timezone
            expires = session.expires_at.astimezone(
                timezone.get_current_timezone()
            ).strftime("%H:%M")
            expires = f" Expires at {expires}"
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
            speed=plan.speed_display if hasattr(plan, 'speed_display') else '',
        )
        result = _send_once(f"hs_welcome:{session.session_id}", phone, msg, ttl=3600)
        _log_sms(phone, msg, msg_type='automated', recipient_name=session.phone_number)
        return result

    @staticmethod
    def hotspot_expiry_warning(session) -> bool:
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
        result = _send_once(f"hotspot_expiry:{session.session_id}", phone, msg, ttl=600)
        _log_sms(phone, msg, msg_type='automated', recipient_name=session.phone_number)
        return result

    @staticmethod
    def hotspot_session_expired(session) -> bool:
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
        result = _send_once(f"hs_expired:{session.session_id}", phone, msg, ttl=3600)
        _log_sms(phone, msg, msg_type='automated', recipient_name=session.phone_number)
        return result

    @staticmethod
    def hotspot_payment_failed(session, reason: str = "") -> bool:
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
        result = _send_once(f"hs_payfail:{session.session_id}", phone, msg, ttl=3600)
        _log_sms(phone, msg, msg_type='automated', recipient_name=session.phone_number)
        return result

    # ─────────────────────────────────────────────────────────────────
    # PPPOE / STATIC
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def pppoe_welcome(customer, username: str = "", password: str = "") -> bool:
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
            username=username,
            password=password,
        )
        result = _send_once(f"pppoe_welcome:{customer.id}", phone, msg, ttl=3600)
        _log_sms(phone, msg, msg_type='automated', recipient_name=name, customer_id=customer.id)
        return result

    @staticmethod
    def pppoe_new_subscription(customer, plan_name: str, amount: float,
                                expires_at=None) -> bool:
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
            plan_name=plan_name,
            username=username,
            password=password,
            expires_at=expiry_str,
            amount=f"{float(amount):,.0f}",
        )
        result = _send_once(f"pppoe_new:{customer.id}", phone, msg, ttl=3600)
        _log_sms(phone, msg, msg_type='automated', recipient_name=name, customer_id=customer.id)
        return result

    @staticmethod
    def pppoe_payment(customer, amount: float, reference: str = "") -> bool:
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
            amount=f"{float(amount):,.0f}",
            reference=reference or '',
            plan_name=plan_name,
        )
        result = _send_once(f"pppoe_pay:{customer.id}:{reference}", phone, msg, ttl=3600)
        _log_sms(phone, msg, msg_type='automated', recipient_name=name, customer_id=customer.id)
        return result

    @staticmethod
    def pppoe_renewal(customer, plan_name: str, expires_at=None) -> bool:
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
            plan_name=plan_name,
            expires_at=expiry_str,
        )
        result = _send_once(f"pppoe_renew:{customer.id}:{expiry_str}", phone, msg, ttl=3600)
        _log_sms(phone, msg, msg_type='automated', recipient_name=name, customer_id=customer.id)
        return result

    @staticmethod
    def pppoe_expiry_reminder(customer, days_left: int, plan_name: str = "") -> bool:
        """Called X days before PPPoE expiry."""
        s = _get_notif_settings()
        if s and not s.pppoe_expiry_reminder:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        if not phone:
            return False
        name = customer.user.first_name or "Customer"
        
        # Try to get expiry date and amount due
        try:
            creds = customer.radius_credentials
            expiry_date = creds.expiration_date.strftime('%d %b %Y') \
                if creds.expiration_date else 'N/A'
            service = customer.services.filter(status='ACTIVE', plan__isnull=False).first()
            amount_due = f"KES {float(service.plan.base_price):,.0f}" if service and service.plan else ''
        except Exception:
            expiry_date = 'N/A'
            amount_due = ''
        
        default_msg = (
            f"Hi {name}, your {plan_name} plan expires in {days_left} day(s) "
            f"({expiry_date}). Renew now{ ' - ' + amount_due if amount_due else ''} to avoid disconnection."
        )
        msg = _get_rendered_message(
            event_type='pppoe_expiry_reminder',
            default_msg=default_msg,
            customer_name=name,
            plan_name=plan_name,
            days_left=days_left,
            expiry_date=expiry_date,
            amount_due=amount_due,
        )
        # Dedup per customer per day (86400s)
        result = _send_once(f"pppoe_expiry:{customer.id}:{days_left}", phone, msg, ttl=86400)
        _log_sms(phone, msg, msg_type='automated', recipient_name=name, customer_id=customer.id)
        return result

    @staticmethod
    def pppoe_suspended(customer, reason: str = "") -> bool:
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
            reason=reason or 'subscription expired',
        )
        result = _send_once(f"pppoe_suspend:{customer.id}", phone, msg, ttl=3600)
        _log_sms(phone, msg, msg_type='automated', recipient_name=name, customer_id=customer.id)
        return result

    @staticmethod
    def pppoe_resumed(customer) -> bool:
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
            plan_name=plan_name,
        )
        result = _send_once(f"pppoe_resume:{customer.id}", phone, msg, ttl=3600)
        _log_sms(phone, msg, msg_type='automated', recipient_name=name, customer_id=customer.id)
        return result

    @staticmethod
    def pppoe_plan_changed(customer, old_plan: str, new_plan: str) -> bool:
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
            old_plan=old_plan,
            new_plan=new_plan,
        )
        result = _send_once(f"pppoe_plan:{customer.id}:{new_plan}", phone, msg, ttl=3600)
        _log_sms(phone, msg, msg_type='automated', recipient_name=name, customer_id=customer.id)
        return result

    @staticmethod
    def pppoe_invoice_issued(customer, invoice) -> bool:
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
            invoice_number=invoice.invoice_number or str(invoice.id),
            amount=f"{float(invoice.total_amount):,.0f}",
            due_date=invoice.due_date.strftime('%d %b %Y') if invoice.due_date else 'N/A',
        )
        result = _send_once(f"invoice:{invoice.id}", phone, msg, ttl=3600)
        _log_sms(phone, msg, msg_type='automated', recipient_name=name, customer_id=customer.id)
        return result

    @staticmethod
    def hotspot_voucher_sold(voucher) -> bool:
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
            result = _send_once(f"voucher_sold:{voucher.id}", phone, msg, ttl=3600)
            _log_sms(phone, msg, msg_type='automated', recipient_name=customer.full_name, customer_id=customer.id)
            return result
        except Exception as exc:
            logger.warning("hotspot_voucher_sold SMS failed: %s", exc)
            return False

    @staticmethod
    def voucher_sold(voucher) -> bool:
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
            result = _send_once(f"voucher:{voucher.id}", phone, msg, ttl=3600)
            _log_sms(phone, msg, msg_type='automated', recipient_name=customer.full_name, customer_id=customer.id)
            return result
        except Exception as exc:
            logger.warning("voucher_sold SMS failed: %s", exc)
            return False


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _customer_phone(customer) -> str:
    """Extract best available phone from a Customer instance."""
    try:
        phone = customer.user.phone_number or ""
        if phone:
            return phone
    except Exception:
        pass
    try:
        return customer.alternative_phone or ""
    except Exception:
        return ""