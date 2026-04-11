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


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_loyalty_notification_sms(self, customer_id, message_type='points_earned', **kwargs):
    """
    Loyalty program SMS notifications.
    message_type: points_earned | redemption | tier_upgrade | expiry_warning
    """
    try:
        from apps.customers.models import Customer
        customer = Customer.objects.select_related('user').get(id=customer_id)
        phone = getattr(customer, 'alternative_phone', '') or ''
        if hasattr(customer.user, 'phone_number'):
            phone = customer.user.phone_number or phone
        if not phone:
            return

        name = customer.user.first_name or 'Customer'

        if message_type == 'points_earned':
            points = kwargs.get('points', 0)
            reason = kwargs.get('reason', '')
            msg = f"Hi {name}, you earned {points} loyalty points! {reason}. Keep it up!"
        elif message_type == 'redemption':
            reward_name = kwargs.get('reward_name', 'a reward')
            voucher_code = kwargs.get('voucher_code', '')
            msg = f"Hi {name}, you redeemed {reward_name}."
            if voucher_code:
                msg += f" Voucher code: {voucher_code}"
        elif message_type == 'tier_upgrade':
            new_tier = kwargs.get('new_tier', '')
            msg = f"Congratulations {name}! You've been upgraded to {new_tier} tier! Enjoy your new benefits."
        elif message_type == 'expiry_warning':
            points = kwargs.get('points', 0)
            days = kwargs.get('days', 30)
            msg = f"Hi {name}, {points} loyalty points will expire in {days} days. Redeem them now!"
        else:
            return

        # Use the generic auto-sms helper (always send loyalty SMS if gateway active)
        from .models import SMSGatewayConfig, SMSMessage
        from .services.gateway_dispatcher import GatewayDispatcher

        config = SMSGatewayConfig.objects.filter(is_active=True).first()
        if not config:
            return

        dispatcher = GatewayDispatcher()
        result = dispatcher.send_sms(to=phone, message=msg)

        SMSMessage.objects.create(
            recipient=phone,
            message=msg,
            status=result.get('status', 'failed'),
            type='automated',
            provider=config.provider,
            provider_message_id=result.get('provider_id', ''),
            cost=result.get('cost', 0),
            error_message=result.get('error', ''),
        )
    except Exception as e:
        logger.error(f"loyalty_notification_sms error: {e}")
        raise self.retry(exc=e)
