import logging
from typing import Any

import requests
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection


logger = logging.getLogger(__name__)


def send_transactional_email(
    *,
    subject: str,
    recipient: str,
    plain_message: str,
    html_message: str | None = None,
    from_email: str | None = None,
) -> dict[str, Any]:
    """
    Send a transactional email with provider fallback.

    Delivery order:
    1. Resend API when configured.
    2. Django email backend (typically SMTP).
    """
    sender = from_email or getattr(settings, "DEFAULT_FROM_EMAIL", "")
    resend_key = getattr(settings, "RESEND_API_KEY", "") or ""
    resend_from = getattr(settings, "RESEND_FROM_EMAIL", "") or sender
    attempts: list[dict[str, str]] = []

    if resend_key and resend_from:
        try:
            response = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {resend_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": resend_from,
                    "to": [recipient],
                    "subject": subject,
                    "html": html_message or plain_message,
                    "text": plain_message,
                },
                timeout=min(int(getattr(settings, "EMAIL_TIMEOUT", 10) or 10), 15),
            )
            if response.status_code < 300:
                payload = response.json() if response.content else {}
                return {
                    "sent": True,
                    "provider": "resend",
                    "message_id": payload.get("id"),
                    "attempts": [{"provider": "resend", "status": "sent"}],
                }
            attempts.append(
                {
                    "provider": "resend",
                    "status": "failed",
                    "error": f"HTTP {response.status_code}: {response.text[:300]}",
                }
            )
        except Exception as exc:
            logger.warning("Resend delivery failed for %s: %s", recipient, exc)
            attempts.append({"provider": "resend", "status": "failed", "error": str(exc)})

    try:
        timeout = int(getattr(settings, "EMAIL_TIMEOUT", 10) or 10)
        connection = get_connection(timeout=timeout)
        message = EmailMultiAlternatives(
            subject=subject,
            body=plain_message,
            from_email=sender,
            to=[recipient],
            connection=connection,
        )
        if html_message:
            message.attach_alternative(html_message, "text/html")

        sent_count = message.send(fail_silently=False)
        if sent_count:
            attempts.append({"provider": "smtp", "status": "sent"})
            return {
                "sent": True,
                "provider": "smtp",
                "message_id": message.extra_headers.get("Message-ID"),
                "attempts": attempts,
            }
        attempts.append(
            {
                "provider": "smtp",
                "status": "failed",
                "error": "Email backend returned zero successful sends.",
            }
        )
    except Exception as exc:
        logger.exception("SMTP delivery failed for %s", recipient)
        attempts.append({"provider": "smtp", "status": "failed", "error": str(exc)})

    final_error = next(
        (attempt.get("error") for attempt in reversed(attempts) if attempt.get("error")),
        "Email delivery failed.",
    )
    return {
        "sent": False,
        "provider": attempts[-1]["provider"] if attempts else "unknown",
        "message_id": None,
        "error": final_error,
        "attempts": attempts,
    }
