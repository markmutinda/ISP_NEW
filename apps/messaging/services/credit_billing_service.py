# apps/messaging/services/credit_billing_service.py
from decimal import Decimal, ROUND_UP
from django.db import transaction
from django.db import connection
from django_tenants.utils import schema_context
# FIX: Use DRF's ValidationError so it returns a clean HTTP 400 instead of crashing (HTTP 500)
from rest_framework.exceptions import ValidationError
from apps.messaging.models import TenantSMSWallet, SMSCreditLedger, SMSGatewayConfig
import logging

logger = logging.getLogger(__name__)


class CreditBillingService:
    """
    Internal wallet billing:
    - debit on send (ONLY if using inbuilt system)
    - topup from your payment flow
    - refund on provider failure
    
    CRITICAL: All methods that query tenant data accept an optional schema_name
    parameter to ensure proper tenant isolation when called from Celery tasks.
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
    def debit_for_sms(message_text: str, sms_message=None, schema_name: str = None) -> Decimal:
        """
        Debit SMS credits from tenant's wallet when using inbuilt gateway.
        
        Args:
            message_text: The SMS message content
            sms_message: Optional SMS message object reference
            schema_name: Explicit tenant schema name (required for Celery tasks)
            
        Returns:
            Decimal: Number of units debited (0 if using external gateway)
            
        Raises:
            ValidationError: If insufficient credits
        """
        from apps.messaging.models import SMSNotificationSettings
        
        # Determine schema: explicit > current connection > fail with warning
        _schema = schema_name or getattr(connection, 'schema_name', None)
        
        if not _schema or _schema == 'public':
            logger.warning("debit_for_sms called without valid schema — skipping deduction")
            return Decimal("0.0000")
        
        # CRITICAL: Execute all queries within the correct tenant schema
        with schema_context(_schema):
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
                logger.debug(f"Schema {_schema} using external gateway — skipping debit")
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
                logger.info(f"Auto-created wallet for schema {_schema}")
            
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
            
            logger.info(f"Debited {units} units from schema {_schema} (remaining: {wallet.sms_units})")
            return units

    @staticmethod
    @transaction.atomic
    def refund_units(units: Decimal, sms_message=None, notes="Provider failure refund", schema_name: str = None):
        """
        Refund SMS credits to tenant's wallet on provider failure.
        
        Args:
            units: Number of units to refund
            sms_message: Optional SMS message object reference
            notes: Reason for refund
            schema_name: Explicit tenant schema name (required for Celery tasks)
        """
        # SAFETY CHECK: If units is 0 (meaning they used an external gateway), do nothing.
        if units <= 0:
            return
        
        # Determine schema: explicit > current connection > fail with warning
        _schema = schema_name or getattr(connection, 'schema_name', None)
        
        if not _schema or _schema == 'public':
            logger.warning(f"refund_units called without valid schema — skipping refund of {units} units")
            return
        
        # CRITICAL: Execute all queries within the correct tenant schema
        with schema_context(_schema):
            wallet = TenantSMSWallet.objects.select_for_update().filter(is_active=True).first()
            if not wallet:
                logger.warning(f"refund_units: No active wallet found for schema {_schema}")
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
            
            logger.info(f"Refunded {units} units to schema {_schema} (new balance: {wallet.sms_units})")

    @staticmethod
    def get_wallet_balance(schema_name: str = None) -> Decimal:
        """
        Get current wallet balance for a tenant.
        
        Args:
            schema_name: Explicit tenant schema name
            
        Returns:
            Decimal: Current SMS units balance
        """
        _schema = schema_name or getattr(connection, 'schema_name', None)
        
        if not _schema or _schema == 'public':
            logger.warning("get_wallet_balance called without valid schema")
            return Decimal("0.0000")
        
        with schema_context(_schema):
            wallet = TenantSMSWallet.objects.filter(is_active=True).first()
            return wallet.sms_units if wallet else Decimal("0.0000")

    @staticmethod
    @transaction.atomic
    def add_credits(units: Decimal, amount_paid: Decimal = None, 
                    reference: str = None, notes: str = None, 
                    schema_name: str = None) -> bool:
        """
        Add credits to tenant's wallet (e.g., after payment).
        
        Args:
            units: Number of SMS units to add
            amount_paid: Optional amount paid (for ledger)
            reference: Optional payment reference
            notes: Optional notes
            schema_name: Explicit tenant schema name
            
        Returns:
            bool: True if successful
        """
        if units <= 0:
            logger.warning(f"add_credits called with invalid units: {units}")
            return False
        
        _schema = schema_name or getattr(connection, 'schema_name', None)
        
        if not _schema or _schema == 'public':
            logger.warning("add_credits called without valid schema")
            return False
        
        with schema_context(_schema):
            wallet = TenantSMSWallet.objects.filter(is_active=True).first()
            if not wallet:
                wallet = TenantSMSWallet.objects.create(
                    sms_units=units,
                    sell_price_per_unit=Decimal('0.4000'),
                    is_active=True
                )
            else:
                wallet = TenantSMSWallet.objects.select_for_update().get(pk=wallet.pk)
                wallet.sms_units = wallet.sms_units + units
                wallet.save(update_fields=["sms_units", "updated_at"])
            
            # Calculate amount if not provided
            if amount_paid is None:
                amount_paid = (units * wallet.sell_price_per_unit).quantize(Decimal("0.01"))
            
            SMSCreditLedger.objects.create(
                wallet=wallet,
                entry_type='credit',
                units=units,
                unit_price=wallet.sell_price_per_unit,
                amount=amount_paid,
                payment_reference=reference,
                notes=notes or f"Credit added: {reference or 'manual'}"
            )
            
            logger.info(f"Added {units} units to schema {_schema} (new balance: {wallet.sms_units})")
            return True