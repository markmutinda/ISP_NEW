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

logger = logging.getLogger(__name__)


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
        dispatcher = GatewayDispatcher()
        result = dispatcher.send_sms(to=phone, message=message)
        if result.get("success"):
            logger.info(f"SMS sent to {phone[:7]}***")
            return True
        logger.warning(f"SMS failed to {phone[:7]}***: {result.get('error')}")
        return False
    except ValueError as e:
        # No active gateway configured
        logger.warning(f"SMS not sent (no gateway): {e}")
        return False
    except Exception as e:
        logger.error(f"SMS dispatch error: {e}", exc_info=True)
        return False


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
        s = _get_settings()
        if not s.hotspot_new_subscription:
            return False
        phone = _fmt_phone(session.phone_number)
        plan = session.plan
        msg = (
            f"Hi! Your WiFi payment of KES {plan.price:.0f} for {plan.name} "
            f"is being processed. You'll get your access code shortly. "
            f"Ref: {session.session_id}"
        )
        return _dispatch(phone, msg)

    @staticmethod
    def hotspot_welcome(session) -> bool:
        """Called after session activates — sends access code & expiry."""
        s = _get_settings()
        if not s.hotspot_welcome:
            return False
        phone = _fmt_phone(session.phone_number)
        plan = session.plan
        expires = ""
        if session.expires_at:
            from django.utils import timezone
            expires = session.expires_at.astimezone(
                timezone.get_current_timezone()
            ).strftime("%H:%M")
            expires = f" Valid till {expires}."
        msg = (
            f"WiFi Active! Code: {session.access_code}. "
            f"Plan: {plan.name} ({plan.duration_display}).{expires} "
            f"Speed: {plan.speed_display}. Enjoy!"
        )
        return _dispatch(phone, msg)

    @staticmethod
    def hotspot_expiry_warning(session) -> bool:
        """Called by Celery task X minutes before expiry."""
        s = _get_settings()
        if not s.hotspot_session_expiry:
            return False
        phone = _fmt_phone(session.phone_number)
        mins = s.hotspot_expiry_minutes_before
        msg = (
            f"Your WiFi session ({session.access_code}) expires in "
            f"{mins} minute(s). Buy another plan to stay connected."
        )
        return _dispatch(phone, msg)

    @staticmethod
    def hotspot_session_expired(session) -> bool:
        """Called when session is marked expired."""
        s = _get_settings()
        if not s.hotspot_session_expired:
            return False
        phone = _fmt_phone(session.phone_number)
        msg = (
            f"Your WiFi session has ended. "
            f"Visit the portal to buy a new plan and reconnect. "
            f"Thank you for using our network!"
        )
        return _dispatch(phone, msg)

    @staticmethod
    def hotspot_payment_failed(session, reason: str = "") -> bool:
        """Called when STK push fails or is cancelled."""
        s = _get_settings()
        if not s.hotspot_payment_failed:
            return False
        phone = _fmt_phone(session.phone_number)
        reason_text = f" ({reason})" if reason else ""
        msg = (
            f"WiFi payment failed{reason_text}. "
            f"Please try again from the portal. No amount was deducted."
        )
        return _dispatch(phone, msg)

    # ─────────────────────────────────────────────────────────────────
    # PPPOE / STATIC
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def pppoe_welcome(customer, username: str = "", password: str = "") -> bool:
        """Called when a new PPPoE/Static service is activated."""
        s = _get_settings()
        if not s.pppoe_welcome:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        name = customer.user.first_name or "Customer"
        cred_text = f" Username: {username} / Password: {password}" if username else ""
        msg = (
            f"Welcome {name}! Your internet service is now active.{cred_text} "
            f"Contact support if you need help."
        )
        return _dispatch(phone, msg)

    @staticmethod
    def pppoe_new_subscription(customer, plan_name: str, amount: float,
                                expires_at=None) -> bool:
        """Called when admin sets up a new subscription."""
        s = _get_settings()
        if not s.pppoe_new_subscription:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        name = customer.user.first_name or "Customer"
        expiry = ""
        if expires_at:
            try:
                expiry = f" Expires: {expires_at.strftime('%d %b %Y')}."
            except Exception:
                pass
        msg = (
            f"Hi {name}, your {plan_name} subscription (KES {amount:.0f}) "
            f"is now active.{expiry} Enjoy your internet!"
        )
        return _dispatch(phone, msg)

    @staticmethod
    def pppoe_payment(customer, amount: float, reference: str = "") -> bool:
        """Called after a PPPoE payment is completed."""
        s = _get_settings()
        if not s.pppoe_payment_confirmation:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        name = customer.user.first_name or "Customer"
        ref = f" Ref: {reference}." if reference else ""
        msg = (
            f"Hi {name}, payment of KES {amount:,.0f} received.{ref} "
            f"Thank you!"
        )
        return _dispatch(phone, msg)

    @staticmethod
    def pppoe_renewal(customer, plan_name: str, expires_at=None) -> bool:
        """Called after successful renewal."""
        s = _get_settings()
        if not s.pppoe_renewal_confirmation:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        name = customer.user.first_name or "Customer"
        expiry = ""
        if expires_at:
            try:
                expiry = f" New expiry: {expires_at.strftime('%d %b %Y')}."
            except Exception:
                pass
        msg = (
            f"Hi {name}, your {plan_name} subscription has been renewed.{expiry} "
            f"Stay connected!"
        )
        return _dispatch(phone, msg)

    @staticmethod
    def pppoe_expiry_reminder(customer, days_left: int, plan_name: str = "") -> bool:
        """Called X days before PPPoE expiry."""
        s = _get_settings()
        if not s.pppoe_expiry_reminder:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        name = customer.user.first_name or "Customer"
        plan_txt = f" ({plan_name})" if plan_name else ""
        msg = (
            f"Hi {name}, your internet subscription{plan_txt} expires in "
            f"{days_left} day(s). Please renew to avoid interruption."
        )
        return _dispatch(phone, msg)

    @staticmethod
    def pppoe_suspended(customer, reason: str = "") -> bool:
        """Called when service is suspended."""
        s = _get_settings()
        if not s.pppoe_service_suspended:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        name = customer.user.first_name or "Customer"
        reason_text = f" Reason: {reason}." if reason else ""
        msg = (
            f"Hi {name}, your internet service has been suspended.{reason_text} "
            f"Please contact support or make a payment to restore service."
        )
        return _dispatch(phone, msg)

    @staticmethod
    def pppoe_resumed(customer) -> bool:
        """Called when service is restored."""
        s = _get_settings()
        if not s.pppoe_service_resumed:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        name = customer.user.first_name or "Customer"
        msg = (
            f"Great news {name}! Your internet service has been restored. "
            f"You should be connected now."
        )
        return _dispatch(phone, msg)

    @staticmethod
    def pppoe_plan_changed(customer, old_plan: str, new_plan: str) -> bool:
        """Called when a plan is changed."""
        s = _get_settings()
        if not s.pppoe_plan_changed:
            return False
        phone = _fmt_phone(_customer_phone(customer))
        name = customer.user.first_name or "Customer"
        msg = (
            f"Hi {name}, your internet plan has been updated from "
            f"{old_plan} to {new_plan}. Enjoy your new plan!"
        )
        return _dispatch(phone, msg)


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