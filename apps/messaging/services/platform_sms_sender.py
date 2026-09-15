import logging
import re
from decimal import Decimal
from typing import Any, Dict

from django.conf import settings
from django.db import transaction
from django_tenants.utils import get_public_schema_name, schema_context
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

    def send_sms(
        self,
        *,
        to: str,
        message: str,
        reference: str = "",
        reminder_delivery_id: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
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

        reservation = self._reserve_platform_units(
            message=message,
            reference=reference,
            reminder_delivery_id=reminder_delivery_id,
            metadata=metadata or {},
        )
        if not reservation.get("success"):
            return {
                "success": False,
                "status": "failed",
                "error": reservation.get("error") or "Insufficient platform SMS balance.",
                "provider": "bytewave_master",
                **reservation,
            }

        try:
            backend = BytewaveBackend(
                api_key=self.api_token,
                sender_id=self.sender_id,
                extra_config={"base_url": self.base_url},
            )
            ok, provider_id, cost = backend.send(to, message)
            if not ok:
                self._refund_platform_units(
                    units=Decimal(str(reservation.get("platform_sms_units") or "0")),
                    reference=reference,
                    debit_ledger_id=reservation.get("platform_sms_ledger_id"),
                    reason="Provider send failure",
                    metadata=metadata or {},
                )
            else:
                self._mark_provider_message(
                    ledger_id=reservation.get("platform_sms_ledger_id"),
                    provider_message_id=provider_id or "",
                )
            return {
                **reservation,
                "success": bool(ok),
                "status": "sent" if ok else "failed",
                "provider_message_id": provider_id or "",
                "cost": str(cost),
                "provider": "bytewave_master",
            }
        except Exception as exc:
            self._refund_platform_units(
                units=Decimal(str(reservation.get("platform_sms_units") or "0")),
                reference=reference,
                debit_ledger_id=reservation.get("platform_sms_ledger_id"),
                reason=f"Provider exception: {exc}",
                metadata=metadata or {},
            )
            logger.exception("Platform SMS send failed: %s", exc)
            return {
                **reservation,
                "success": False,
                "status": "failed",
                "error": str(exc),
                "provider": "bytewave_master",
            }

    def _sms_units_for_message(self, text: str) -> Decimal:
        length = len(text or "")
        if length <= 160:
            return Decimal("1.0000")
        segments = 1 + ((length - 160 + 152) // 153)
        return Decimal(str(segments)).quantize(Decimal("0.0001"))

    def _reserve_platform_units(
        self,
        *,
        message: str,
        reference: str = "",
        reminder_delivery_id: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        units = self._sms_units_for_message(message)
        try:
            with schema_context(get_public_schema_name()):
                from apps.subscriptions.models import (
                    PlatformSMSLedger,
                    PlatformSMSWallet,
                    SubscriptionInvoiceReminderDelivery,
                )

                with transaction.atomic():
                    wallet = PlatformSMSWallet.get_active()
                    wallet = PlatformSMSWallet.objects.select_for_update().get(pk=wallet.pk)
                    if wallet.enforce_balance and wallet.sms_units < units:
                        return {
                            "success": False,
                            "error": "Insufficient Netily platform SMS balance.",
                            "platform_sms_units": str(units),
                            "platform_sms_wallet_balance": str(wallet.sms_units),
                        }

                    if wallet.enforce_balance:
                        wallet.sms_units = wallet.sms_units - units
                        wallet.save(update_fields=["sms_units", "updated_at"])

                    reminder_delivery = None
                    if reminder_delivery_id:
                        reminder_delivery = SubscriptionInvoiceReminderDelivery.objects.filter(
                            pk=reminder_delivery_id,
                        ).first()

                    ledger = PlatformSMSLedger.objects.create(
                        wallet=wallet,
                        entry_type="debit",
                        units=-units,
                        unit_price=wallet.sell_price_per_unit,
                        amount=(units * wallet.sell_price_per_unit).quantize(Decimal("0.01")),
                        reference=reference or str(reminder_delivery_id or ""),
                        notes="Subscription reminder SMS debit",
                        metadata=metadata or {},
                        reminder_delivery=reminder_delivery,
                    )
                    return {
                        "success": True,
                        "platform_sms_units": str(units),
                        "platform_sms_ledger_id": str(ledger.id),
                        "platform_sms_wallet_balance": str(wallet.sms_units),
                        "platform_sms_balance_enforced": wallet.enforce_balance,
                    }
        except Exception as exc:
            logger.exception("Platform SMS wallet debit failed: %s", exc)
            return {
                "success": False,
                "error": str(exc),
                "platform_sms_units": str(units),
            }

    def _refund_platform_units(
        self,
        *,
        units: Decimal,
        reference: str = "",
        debit_ledger_id: str | None = None,
        reason: str = "Provider failure refund",
        metadata: Dict[str, Any] | None = None,
    ):
        if units <= 0:
            return
        try:
            with schema_context(get_public_schema_name()):
                from apps.subscriptions.models import PlatformSMSLedger, PlatformSMSWallet

                with transaction.atomic():
                    wallet = PlatformSMSWallet.get_active()
                    wallet = PlatformSMSWallet.objects.select_for_update().get(pk=wallet.pk)
                    if wallet.enforce_balance:
                        wallet.sms_units = wallet.sms_units + units
                        wallet.save(update_fields=["sms_units", "updated_at"])
                    PlatformSMSLedger.objects.create(
                        wallet=wallet,
                        entry_type="refund",
                        units=units,
                        unit_price=wallet.sell_price_per_unit,
                        amount=(units * wallet.sell_price_per_unit).quantize(Decimal("0.01")),
                        reference=reference,
                        notes=reason,
                        metadata={
                            **(metadata or {}),
                            "debit_ledger_id": str(debit_ledger_id or ""),
                        },
                    )
        except Exception as exc:
            logger.exception("Platform SMS wallet refund failed: %s", exc)

    def _mark_provider_message(self, *, ledger_id: str | None, provider_message_id: str):
        if not ledger_id or not provider_message_id:
            return
        try:
            with schema_context(get_public_schema_name()):
                from apps.subscriptions.models import PlatformSMSLedger

                PlatformSMSLedger.objects.filter(pk=ledger_id).update(
                    provider_message_id=str(provider_message_id),
                )
        except Exception as exc:
            logger.warning("Platform SMS ledger provider update failed: %s", exc)

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
