# apps/core/telegram_notify.py
import logging
import requests
from datetime import datetime
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _mask_phone(phone: str) -> str:
    """Mask phone number for privacy in success messages."""
    phone = phone or ""
    if len(phone) < 8:
        return phone
    return f"{phone[:6]}****{phone[-2:]}"


def _fmt_datetime(dt=None) -> str:
    """Format datetime to match Tuma's style: 'Thursday, 27th of August 2026 11:44PM EAT'"""
    dt = dt or timezone.localtime(timezone.now())
    day = dt.day
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return dt.strftime(f"%A, {day}{suffix} of %B %Y %I:%M%p EAT")


def send_telegram_payment_alert(message: str):
    """
    Send a payment alert via Telegram using the dedicated payments bot.
    """
    token = getattr(settings, "TELEGRAM_PAYMENTS_BOT_TOKEN", "")
    chat_ids = getattr(settings, "TELEGRAM_PAYMENTS_ADMIN_CHAT_IDS", [])
    
    if not token or not chat_ids:
        logger.warning("Telegram payment alert skipped: bot token or chat IDs not configured")
        return
    
    for chat_id in chat_ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=8,
            )
        except Exception as e:
            logger.warning(f"Telegram payment alert failed: {e}")


def build_payment_success_message(*, receipt, amount, phone, tenant_label, dt=None):
    """
    Build success message matching Tuma's style.
    Phone is masked for privacy.
    """
    return (
        f"{receipt} confirmed. API Payment received: KES {amount} "
        f"from {_mask_phone(phone)} for {tenant_label} on {_fmt_datetime(dt)}.\n"
        f"Thank you for choosing Netily Payment solutions."
    )


def build_payment_failure_message(*, phone, amount, tenant_label, reason, dt=None):
    """
    Build failure message matching Tuma's style.
    Phone is shown in full for failures (matching Tuma's behavior).
    """
    return (
        f"API Payment failed from {phone} for {tenant_label}. "
        f"Amount: KES {amount}. Reason: {reason} on {_fmt_datetime(dt)}.\n"
        f"Thank you for choosing Netily Payment solutions."
    )


# ── Daraja result code mapping to human-readable messages ──
DARAJA_FAILURE_REASONS = {
    "1032": "Transaction cancelled by user",
    "1": "Insufficient balance in M-Pesa account",
    "1037": "Timeout - no response from user",
    "2001": "Invalid M-Pesa PIN entered",
    "1025": "Duplicated MSISDN. MSISDN has an existing USSD Session",
    "1019": "Error occurred while sending push request",
}


def humanize_daraja_result(result_code, fallback=""):
    """Convert Daraja result codes to human-readable messages."""
    return DARAJA_FAILURE_REASONS.get(str(result_code), fallback or "Transaction failed")