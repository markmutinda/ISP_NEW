# apps/messaging/services/credit_billing_service.py
from decimal import Decimal, ROUND_UP
from django.db import transaction
# FIX: Use DRF's ValidationError so it returns a clean HTTP 400 instead of crashing (HTTP 500)
from rest_framework.exceptions import ValidationError
from apps.messaging.models import TenantSMSWallet, SMSCreditLedger, SMSGatewayConfig

class CreditBillingService:
    """
    Internal wallet billing:
    - debit on send (ONLY if using inbuilt system)
    - topup from your payment flow
    - refund on provider failure
    """

    @staticmethod
    def sms_units_for_message(text: str) -> Decimal:
        """
        Calculate SMS units required for a message.
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
        from apps.messaging.models import SMSNotificationSettings
        
        # 1. Check if they are using an external gateway
        gateway = SMSGatewayConfig.objects.filter(is_active=True).first()
        
        # Determine inbuilt from EITHER source — notification settings is the
        # user-facing toggle; gateway config should mirror it but may drift.
        try:
            notif_settings = SMSNotificationSettings.get_settings()
            notif_inbuilt = notif_settings.use_inbuilt_system if notif_settings else False
        except Exception:
            notif_inbuilt = False
        
        gateway_inbuilt = gateway.use_inbuilt_system if gateway else False
        
        # Use inbuilt if EITHER source says true (this prevents drift issues)
        use_inbuilt = notif_inbuilt or gateway_inbuilt
        
        # If they are NOT using the inbuilt system (either source says false),
        # they are using their own provider keys — no wallet deduction.
        if not use_inbuilt:
            return Decimal("0.0000")

        # 2. If using Inbuilt System, proceed with wallet deduction
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
            # FIX: This now returns a clean 400 Bad Request to the frontend
            raise ValidationError({
                "detail": "Insufficient SMS credits in platform wallet. Please top up your account to send messages."
            })

        wallet.sms_units = wallet.sms_units - units
        wallet.save(update_fields=["sms_units", "updated_at"])

        SMSCreditLedger.objects.create(
            wallet=wallet,
            entry_type='debit',
            units=-units,
            unit_price=wallet.sell_price_per_unit,
            amount=(units * wallet.sell_price_per_unit).quantize(Decimal("0.01")),
            sms_message=sms_message,
            notes='SMS send debit (Inbuilt Gateway)'
        )
        return units

    @staticmethod
    @transaction.atomic
    def refund_units(units: Decimal, sms_message=None, notes="Provider failure refund"):
        # SAFETY CHECK: If units is 0 (meaning they used an external gateway), do nothing.
        if units <= 0:
            return

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