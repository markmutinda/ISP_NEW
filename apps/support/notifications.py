import logging

from apps.core.event_sms import queue_event_sms

logger = logging.getLogger(__name__)


def _target(ticket):
    user = getattr(ticket.customer, "user", None)
    phone = (getattr(user, "phone_number", "") or "").strip()
    name = (getattr(user, "first_name", "") or "").strip() or "Customer"
    return phone, name


def notify_ticket_created(ticket, company_name=""):
    try:
        phone, name = _target(ticket)
        if not phone:
            return
        sign = f" - {company_name}" if company_name else ""
        msg = (
            f"Hi {name}, we have received your issue and logged it as ticket "
            f"{ticket.ticket_number} ({(ticket.subject or '')[:60]}). "
            f"Our team will get back to you shortly.{sign}"
        )
        queue_event_sms(phone, msg, recipient_name=name, msg_type="ticket")
    except Exception:
        logger.exception("Ticket-created SMS failed for %s", getattr(ticket, "pk", None))


def notify_ticket_resolved(ticket, company_name=""):
    try:
        phone, name = _target(ticket)
        if not phone:
            return
        sign = f" - {company_name}" if company_name else ""
        msg = (
            f"Hi {name}, your ticket {ticket.ticket_number} ({(ticket.subject or '')[:60]}) "
            f"has been marked resolved. If the problem persists, reply or contact us.{sign}"
        )
        queue_event_sms(phone, msg, recipient_name=name, msg_type="ticket")
    except Exception:
        logger.exception("Ticket-resolved SMS failed for %s", getattr(ticket, "pk", None))