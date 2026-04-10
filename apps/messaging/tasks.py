"""
Automated SMS trigger tasks.
Each task checks the tenant's SMSGatewayConfig for the relevant
auto_* flag before sending.
"""
import logging
from celery import shared_task
from decimal import Decimal

logger = logging.getLogger(__name__)


def _send_auto_sms(phone: str, message: str, trigger_flag: str, msg_type='automated'):
    """
    Internal helper: send an SMS only if the active gateway has the trigger enabled.
    Returns True if sent, False if skipped or failed.
    """
    from .models import SMSGatewayConfig, SMSMessage
    from .services.gateway_dispatcher import GatewayDispatcher

    config = SMSGatewayConfig.objects.filter(is_active=True).first()
    if not config:
        logger.debug("No active SMS gateway — skipping auto SMS")
        return False

    if not getattr(config, trigger_flag, False):
        logger.debug(f"Trigger {trigger_flag} is disabled — skipping")
        return False

    try:
        dispatcher = GatewayDispatcher()
        result = dispatcher.send_sms(to=phone, message=message)

        SMSMessage.objects.create(
            recipient=phone,
            message=message,
            status=result.get('status', 'failed'),
            type=msg_type,
            provider=config.provider,
            provider_message_id=result.get('provider_id', ''),
            cost=result.get('cost', Decimal('0.00')),
            error_message=result.get('error', ''),
        )
        return result.get('success', False)
    except Exception as e:
        logger.error(f"Auto SMS ({trigger_flag}) failed for {phone}: {e}")
        return False


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_payment_confirmation_sms(self, customer_id, amount, reference=''):
    """Triggered after a payment is marked COMPLETED."""
    try:
        from apps.customers.models import Customer
        customer = Customer.objects.select_related('user').get(id=customer_id)
        phone = customer.phone_number or customer.user.phone_number
        if not phone:
            return

        name = customer.user.first_name or 'Customer'
        msg = (
            f"Hi {name}, your payment of KES {amount:,.2f} has been received. "
            f"Ref: {reference}. Thank you!"
        )
        _send_auto_sms(phone, msg, 'auto_payment_confirmation')
    except Exception as e:
        logger.error(f"payment_confirmation_sms error: {e}")
        raise self.retry(exc=e)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_welcome_sms(self, customer_id):
    """Triggered when a new customer is created."""
    try:
        from apps.customers.models import Customer
        customer = Customer.objects.select_related('user').get(id=customer_id)
        phone = customer.phone_number or customer.user.phone_number
        if not phone:
            return

        name = customer.user.first_name or 'Customer'
        msg = (
            f"Welcome {name}! Your account has been set up. "
            f"Contact support if you need any help. Enjoy your service!"
        )
        _send_auto_sms(phone, msg, 'auto_welcome_message')
    except Exception as e:
        logger.error(f"welcome_sms error: {e}")
        raise self.retry(exc=e)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_expiry_reminder_sms(self, customer_id, days_left=2):
    """Triggered before a subscription/plan expires."""
    try:
        from apps.customers.models import Customer
        customer = Customer.objects.select_related('user').get(id=customer_id)
        phone = customer.phone_number or customer.user.phone_number
        if not phone:
            return

        name = customer.user.first_name or 'Customer'
        msg = (
            f"Hi {name}, your internet plan expires in {days_left} day(s). "
            f"Please renew to avoid service interruption."
        )
        _send_auto_sms(phone, msg, 'auto_expiry_reminder')
    except Exception as e:
        logger.error(f"expiry_reminder_sms error: {e}")
        raise self.retry(exc=e)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_service_suspension_sms(self, customer_id, reason=''):
    """Triggered when a customer's service is suspended."""
    try:
        from apps.customers.models import Customer
        customer = Customer.objects.select_related('user').get(id=customer_id)
        phone = customer.phone_number or customer.user.phone_number
        if not phone:
            return

        name = customer.user.first_name or 'Customer'
        reason_text = f" Reason: {reason}" if reason else ""
        msg = (
            f"Hi {name}, your internet service has been suspended.{reason_text} "
            f"Please contact support or make a payment to restore service."
        )
        _send_auto_sms(phone, msg, 'auto_service_suspension')
    except Exception as e:
        logger.error(f"service_suspension_sms error: {e}")
        raise self.retry(exc=e)
