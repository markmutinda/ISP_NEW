import logging

from django.conf import settings
from django.db import connection, transaction

logger = logging.getLogger(__name__)


def truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def request_company_name(request) -> str:
    company = getattr(request, "company", None)
    return (
        getattr(company, "name", "")
        or getattr(getattr(request, "user", None), "company_name", "")
        or ""
    ).strip()


def deliver_event_sms(phone, message, recipient_name="", msg_type="notification") -> bool:
    """Send through the tenant's active SMS gateway and log it. Never raises."""
    from apps.messaging.services.notification_sender import _dispatch, _log_sms

    try:
        sent = bool(_dispatch(phone, message))
    except Exception:
        logger.exception("Event SMS dispatch failed for %s", phone)
        sent = False
    try:
        _log_sms(
            phone, message,
            status="sent" if sent else "failed",
            msg_type=msg_type,
            recipient_name=recipient_name,
        )
    except Exception:
        logger.warning("Event SMS log failed for %s", phone, exc_info=True)
    return sent


def queue_event_sms(phone, message, *, recipient_name="", msg_type="notification"):
    """Queue SMS on Celery after the DB commit; fall back to inline send if the broker is down."""
    phone = (phone or "").strip()
    if not phone or not message:
        return
    schema = connection.schema_name

    def _go():
        try:
            from apps.customers.tasks import send_event_sms_task
            send_event_sms_task.apply_async(
                args=[schema, phone, message, recipient_name, msg_type],
                queue="default",
                retry=False,
            )
        except Exception:
            logger.exception("Could not queue event SMS; sending inline")
            deliver_event_sms(phone, message, recipient_name, msg_type)

    transaction.on_commit(_go)


def build_staff_credentials_sms(user, password, *, company_name="", subdomain="", password_reset=False) -> str:
    first = (user.first_name or "there").strip()
    org = company_name or "your ISP"
    base = getattr(settings, "TENANT_BASE_DOMAIN", "") or ""
    url = f"https://{subdomain}.{base}/admin/login" if subdomain and base and base != "localhost" else ""
    if password_reset:
        parts = [f"Hi {first}, your {org} dashboard password was reset by an administrator."]
    else:
        parts = [f"Hi {first}, your {org} staff account is ready. Role: {user.get_role_display_name()}."]
    if url:
        parts.append(f"Login: {url}")
    parts.append(f"Email: {user.email}")
    parts.append(f"Password: {password}")
    parts.append("Please change your password after signing in.")
    return " ".join(parts)