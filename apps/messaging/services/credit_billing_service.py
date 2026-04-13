# apps/messaging/services/credit_billing_service.py
from decimal import Decimal, ROUND_UP
from django.db import transaction
from django.core.exceptions import ValidationError
from apps.messaging.models import TenantSMSWallet, SMSCreditLedger

class CreditBillingService:
    """
    Internal wallet billing:
    - debit on send
    - topup from your payment flow
    - refund on provider failure
    """

    @staticmethod
    def sms_units_for_message(text: str) -> Decimal:
        # simple GSM rule approximation:
        # 1 unit = up to 160 chars
        length = len(text or "")
        if length <= 160:
            return Decimal("1")
        segments = (length + 152) // 153
        return Decimal(str(segments))

    @staticmethod
    @transaction.atomic
    def debit_for_sms(message_text: str, sms_message=None) -> Decimal:
        wallet = TenantSMSWallet.objects.select_for_update().filter(is_active=True).first()
        if not wallet:
            raise ValidationError("SMS wallet not configured.")

        units = CreditBillingService.sms_units_for_message(message_text)
        if wallet.sms_units < units:
            raise ValidationError("Insufficient SMS credits.")

        wallet.sms_units = wallet.sms_units - units
        wallet.save(update_fields=["sms_units", "updated_at"])

        SMSCreditLedger.objects.create(
            wallet=wallet,
            entry_type='debit',
            units=-units,
            unit_price=wallet.sell_price_per_unit,
            amount=(units * wallet.sell_price_per_unit).quantize(Decimal("0.01")),
            sms_message=sms_message,
            notes='SMS send debit'
        )
        return units

    @staticmethod
    @transaction.atomic
    def refund_units(units: Decimal, sms_message=None, notes="Provider failure refund"):
        wallet = TenantSMSWallet.objects.select_for_update().filter(is_active=True).first()
        if not wallet:
            return
        wallet.sms_units = wallet.sms_units + units
        wallet.save(update_fields=["sms_units", "updated_at"])

        SMSCreditLedger.objects.create(
            wallet=wallet,
            entry_type='refund',
            units=units,
            unit_price=wallet.sell_price_per_unit,
            amount=(units * wallet.sell_price_per_unit).quantize(Decimal("0.01")),
            sms_message=sms_message,
            notes=notes
        )