import logging
from typing import Any, Dict

from django.conf import settings

from apps.messaging.services.gateway_dispatcher import BytewaveBackend

logger = logging.getLogger(__name__)


class PlatformSMSSender:
    """
    Sends Netily-owned platform SMS from the master Bytewave account.

    This is intentionally separate from GatewayDispatcher because tenant customer
    SMS may debit TenantSMSWallet balances, while platform subscription reminders
    should use the shared master SMS balance only.
    """

    def __init__(self):
        self.api_token = getattr(settings, "BYTEWAVE_API_TOKEN", "") or ""
        self.sender_id = getattr(settings, "BYTEWAVE_SENDER_ID", "BytewaveSMS") or "BytewaveSMS"
        self.base_url = getattr(settings, "BYTEWAVE_BASE_URL", "https://portal.bytewavenetworks.com/api/v3")

    def send_sms(self, *, to: str, message: str) -> Dict[str, Any]:
        if not self.api_token:
            return {
                "success": False,
                "status": "failed",
                "error": "BYTEWAVE_API_TOKEN is not configured.",
            }
        if not to:
            return {
                "success": False,
                "status": "failed",
                "error": "Recipient phone number is required.",
            }

        try:
            backend = BytewaveBackend(
                api_key=self.api_token,
                sender_id=self.sender_id,
                extra_config={"base_url": self.base_url},
            )
            ok, provider_id, cost = backend.send(to, message)
            return {
                "success": bool(ok),
                "status": "sent" if ok else "failed",
                "provider_message_id": provider_id or "",
                "cost": str(cost),
                "provider": "bytewave_master",
            }
        except Exception as exc:
            logger.exception("Platform SMS send failed: %s", exc)
            return {
                "success": False,
                "status": "failed",
                "error": str(exc),
                "provider": "bytewave_master",
            }

    def get_balance(self) -> Dict[str, Any]:
        if not self.api_token:
            return {
                "success": False,
                "error": "BYTEWAVE_API_TOKEN is not configured.",
                "balance": 0,
                "currency": "SMS_UNITS",
            }

        try:
            backend = BytewaveBackend(
                api_key=self.api_token,
                sender_id=self.sender_id,
                extra_config={"base_url": self.base_url},
            )
            result = backend.get_balance()
            return {
                "success": True,
                "balance": float(result.get("balance") or 0),
                "currency": result.get("currency") or "SMS_UNITS",
                "raw": result,
                "provider": "bytewave_master",
            }
        except Exception as exc:
            logger.exception("Platform SMS balance fetch failed: %s", exc)
            return {
                "success": False,
                "error": str(exc),
                "balance": 0,
                "currency": "SMS_UNITS",
                "provider": "bytewave_master",
            }
