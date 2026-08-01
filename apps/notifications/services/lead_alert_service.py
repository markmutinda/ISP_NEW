"""Reliable, referral-aware delivery for public lead alerts."""

import logging
from html import escape

from django_tenants.utils import get_public_schema_name, schema_context

from apps.affiliate.services import affiliate_lead_data
from apps.core.models import Lead

from .telegram_service import TelegramService

logger = logging.getLogger(__name__)


def build_lead_alert_message(lead, affiliate_referral=None) -> str:
    """Build safe Telegram HTML from a lead and its trusted attribution."""
    affiliate_name = "Not an affiliate referral"
    affiliate_code = "Not applicable"
    if affiliate_referral:
        affiliate_name = affiliate_referral.get("affiliate_name") or "Unknown affiliate"
        affiliate_code = affiliate_referral.get("referral_code") or "Unknown"

    values = {
        "name": lead.name or "Not specified",
        "company": lead.company_name or "Not specified",
        "phone": lead.phone or "Not specified",
        "email": lead.email or "Not specified",
        "source": lead.lead_source or "Not specified",
        "referred_by": lead.referral_name or affiliate_name,
        "affiliate_code": affiliate_code,
        "message": lead.message or "No message provided",
    }
    safe = {key: escape(str(value), quote=True) for key, value in values.items()}

    return (
        "🚨 <b>NEW ISP LEAD</b> 🚨\n\n"
        f"<b>Lead ID:</b> {lead.pk}\n"
        f"👤 <b>Name:</b> {safe['name']}\n"
        f"🏢 <b>Company:</b> {safe['company']}\n"
        f"📞 <b>Phone:</b> {safe['phone']}\n"
        f"✉️ <b>Email:</b> {safe['email']}\n"
        f"📍 <b>Source:</b> {safe['source']}\n"
        f"🤝 <b>Referred by:</b> {safe['referred_by']}\n"
        f"🔗 <b>Affiliate code:</b> {safe['affiliate_code']}\n\n"
        f"<b>Message:</b> {safe['message']}\n\n"
        "<i>Open Superadmin → Leads to review and follow up.</i>"
    )


def deliver_telegram_lead_alert(lead_id: int) -> bool:
    """Load a lead from the public schema and deliver its complete alert."""
    with schema_context(get_public_schema_name()):
        try:
            lead = Lead.objects.prefetch_related(
                "affiliatereferral_set__affiliate__user"
            ).get(pk=lead_id)
        except Lead.DoesNotExist:
            logger.error("Cannot send Telegram alert: lead %s does not exist", lead_id)
            return False
        message = build_lead_alert_message(lead, affiliate_lead_data(lead))

    return TelegramService().send_message_to_admins(message)
