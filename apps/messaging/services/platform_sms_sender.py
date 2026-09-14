import logging
import re
from decimal import Decimal
from typing import Any, Dict

from django.conf import settings
import requests

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
            response = {
                "success": True,
                "balance": float(result.get("balance") or 0),
                "currency": result.get("currency") or "SMS_UNITS",
                "raw": result,
                "provider": "bytewave_master",
            }
            if response["balance"] > 0:
                return response

            legacy = self._get_legacy_http_balance()
            if legacy.get("success") and float(legacy.get("balance") or 0) > 0:
                return legacy
            return response
        except Exception as exc:
            logger.warning("Platform SMS v3 balance fetch failed: %s", exc)
            legacy = self._get_legacy_http_balance()
            if legacy.get("success"):
                return legacy
            legacy["error"] = legacy.get("error") or str(exc)
            return legacy

    def _get_legacy_http_balance(self) -> Dict[str, Any]:
        attempts = (
            ("get_params", requests.get, {"params": {"api_token": self.api_token}}),
            ("post_json", requests.post, {"json": {"api_token": self.api_token}}),
            ("get_json", requests.get, {"json": {"api_token": self.api_token}}),
        )
        last_error = ""
        for label, method, payload_kwargs in attempts:
            result = self._request_legacy_http_balance(label, method, payload_kwargs)
            if result.get("success"):
                return result
            last_error = result.get("error") or last_error

        return {
            "success": False,
            "error": last_error or "Unable to read Bytewave legacy balance.",
            "balance": 0,
            "currency": "SMS_UNITS",
            "provider": "bytewave_master_legacy",
        }

    def _request_legacy_http_balance(self, label, method, payload_kwargs) -> Dict[str, Any]:
        try:
            resp = method(
                "https://portal.bytewavenetworks.com/api/http/balance",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=15,
                **payload_kwargs,
            )
            data = resp.json()
            if resp.status_code >= 400:
                return {
                    "success": False,
                    "error": data.get("message", f"HTTP {resp.status_code}"),
                    "balance": 0,
                    "currency": "SMS_UNITS",
                    "provider": "bytewave_master_legacy",
                    "raw": data,
                }
            raw = data.get("data", data)
            units = self._extract_units(raw) or Decimal("0")
            currency = "KES" if self._payload_contains_kes(raw) else "SMS_UNITS"
            return {
                "success": True,
                "balance": float(units),
                "currency": currency,
                "provider": f"bytewave_master_legacy_{label}",
                "raw": raw,
            }
        except Exception as exc:
            logger.warning("Platform SMS legacy balance fetch failed via %s: %s", label, exc)
            return {
                "success": False,
                "error": str(exc),
                "balance": 0,
                "currency": "SMS_UNITS",
                "provider": f"bytewave_master_legacy_{label}",
            }

    def _extract_units(self, payload):
        keys = (
            "sms_unit", "sms_units", "smsunit", "units", "unit",
            "remaining_balance", "available_units", "remaining_units",
            "wallet_balance", "balance", "remaining", "available",
            "credit", "credits",
        )
        if isinstance(payload, dict):
            for key in keys:
                value = payload.get(key)
                if value not in (None, ""):
                    parsed = self._parse_decimal(value)
                    if parsed is not None:
                        return parsed
            for value in payload.values():
                found = self._extract_units(value)
                if found is not None:
                    return found
            return None
        if isinstance(payload, (list, tuple)):
            for value in payload:
                found = self._extract_units(value)
                if found is not None:
                    return found
            return None
        try:
            if payload in (None, ""):
                return None
            return self._parse_decimal(payload)
        except Exception:
            return None

    def _parse_decimal(self, value):
        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except Exception:
            match = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", str(value))
            if not match:
                return None
            try:
                return Decimal(match.group(0).replace(",", ""))
            except Exception:
                return None

    def _payload_contains_kes(self, payload):
        if isinstance(payload, dict):
            return any(self._payload_contains_kes(value) for value in payload.values())
        if isinstance(payload, (list, tuple)):
            return any(self._payload_contains_kes(value) for value in payload)
        return "ksh" in str(payload).lower() or "kes" in str(payload).lower()
