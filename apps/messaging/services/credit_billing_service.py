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
        """
        Calculate SMS units required for a message.
        
        Rules:
        - First segment: up to 160 characters
        - Subsequent segments: up to 153 characters each
        - Multi-part SMS: first part 160, subsequent 153 chars each
        
        Example:
        - 0-160 chars -> 1 unit
        - 161-313 chars -> 2 units (160 + 153)
        - 314-466 chars -> 3 units (160 + 153 + 153)
        """
        length = len(text or "")
        if length <= 160:
            return Decimal("1")
        # Multi-part SMS: first part 160, subsequent 153 chars each
        segments = 1 + ((length - 160 + 152) // 153)
        return Decimal(str(segments))

    @staticmethod
    @transaction.atomic
    def debit_for_sms(message_text: str, sms_message=None) -> Decimal:
        # FIX 3: Auto-create wallet if it doesn't exist (tenant just hasn't topped up yet)
        wallet = TenantSMSWallet.objects.filter(is_active=True).first()
        if not wallet:
            # Auto-create wallet with zero balance — tenant just hasn't topped up yet
            wallet = TenantSMSWallet.objects.create(
                sms_units=Decimal('0.0000'),
                sell_price_per_unit=Decimal('0.4000'),
                is_active=True
            )
        wallet = TenantSMSWallet.objects.select_for_update().get(pk=wallet.pk)

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