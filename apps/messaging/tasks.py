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


def _get_rendered_message(event_type: str, default_msg: str, **context) -> str:
    """
    Fetches the active custom template for a given event from the database.
    Replaces {variable} placeholders with actual values.
    Falls back to the hardcoded default_msg if no template exists.
    """
    from apps.messaging.models import SMSTemplate
    
    # Query the database for the user's saved template for this specific event
    template = SMSTemplate.objects.filter(event_type=event_type, is_active=True).first()
    
    if not template or not template.content.strip():
        return default_msg
        
    msg = template.content
    # Dynamically inject the context variables (e.g., {name}, {amount}) into the text
    for key, value in context.items():
        msg = msg.replace(f"{{{key}}}", str(value))
        
    return msg


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_payment_confirmation_sms(self, customer_id, amount, reference=''):
    """
    Triggered after a payment is marked COMPLETED.
    Uses dynamic template from DB with event_type='pppoe_payment'
    """
    try:
        from apps.customers.models import Customer
        customer = Customer.objects.select_related('user').get(id=customer_id)
        phone = customer.phone_number or customer.user.phone_number
        if not phone:
            return

        name = customer.user.first_name or 'Customer'
        default_msg = f"Hi {name}, your payment of KES {amount:,.2f} has been received. Ref: {reference}. Thank you!"
        
        # Pull from DB: event_type must match the value saved from the frontend React Select component
        msg = _get_rendered_message(
            event_type='pppoe_payment',
            default_msg=default_msg,
            name=name,
            amount=f"{amount:,.2f}",
            reference=reference
        )
        
        _send_auto_sms(phone, msg, 'auto_payment_confirmation')
    except Exception as e:
        logger.error(f"payment_confirmation_sms error: {e}")
        raise self.retry(exc=e)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_welcome_sms(self, customer_id):
    """
    Triggered when a new customer is created.
    Uses dynamic template from DB with event_type='pppoe_welcome'
    """
    try:
        from apps.customers.models import Customer
        customer = Customer.objects.select_related('user').get(id=customer_id)
        phone = customer.phone_number or customer.user.phone_number
        if not phone:
            return

        name = customer.user.first_name or 'Customer'
        default_msg = f"Welcome {name}! Your account has been set up. Contact support if you need any help. Enjoy your service!"
        
        msg = _get_rendered_message(
            event_type='pppoe_welcome',
            default_msg=default_msg,
            name=name
        )
        
        _send_auto_sms(phone, msg, 'auto_welcome_message')
    except Exception as e:
        logger.error(f"welcome_sms error: {e}")
        raise self.retry(exc=e)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_expiry_reminder_sms(self, customer_id, days_left=2):
    """
    Triggered before a subscription/plan expires.
    Uses dynamic template from DB with event_type='pppoe_expiry'
    """
    try:
        from apps.customers.models import Customer
        customer = Customer.objects.select_related('user').get(id=customer_id)
        phone = customer.phone_number or customer.user.phone_number
        if not phone:
            return

        name = customer.user.first_name or 'Customer'
        default_msg = f"Hi {name}, your internet plan expires in {days_left} day(s). Please renew to avoid service interruption."
        
        msg = _get_rendered_message(
            event_type='pppoe_expiry',
            default_msg=default_msg,
            name=name,
            days_left=days_left
        )
        
        _send_auto_sms(phone, msg, 'auto_expiry_reminder')
    except Exception as e:
        logger.error(f"expiry_reminder_sms error: {e}")
        raise self.retry(exc=e)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_service_suspension_sms(self, customer_id, reason=''):
    """
    Triggered when a customer's service is suspended.
    Uses dynamic template from DB with event_type='pppoe_suspended'
    """
    try:
        from apps.customers.models import Customer
        customer = Customer.objects.select_related('user').get(id=customer_id)
        phone = customer.phone_number or customer.user.phone_number
        if not phone:
            return

        name = customer.user.first_name or 'Customer'
        default_msg = f"Hi {name}, your internet service has been suspended. Reason: {reason} Please contact support or make a payment to restore service." if reason else f"Hi {name}, your internet service has been suspended. Please contact support or make a payment to restore service."
        
        msg = _get_rendered_message(
            event_type='pppoe_suspended',
            default_msg=default_msg,
            name=name,
            reason=reason
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
    Uses dynamic template from DB with event_type='loyalty_{message_type}'
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
        
        # Build default messages and context based on message_type
        if message_type == 'points_earned':
            points = kwargs.get('points', 0)
            reason = kwargs.get('reason', '')
            default_msg = f"Hi {name}, you earned {points} loyalty points! {reason}. Keep it up!"
            context = {
                'name': name,
                'points': points,
                'reason': reason
            }
            event_type = 'loyalty_points_earned'
        elif message_type == 'redemption':
            reward_name = kwargs.get('reward_name', 'a reward')
            voucher_code = kwargs.get('voucher_code', '')
            default_msg = f"Hi {name}, you redeemed {reward_name}."
            if voucher_code:
                default_msg += f" Voucher code: {voucher_code}"
            context = {
                'name': name,
                'reward_name': reward_name,
                'voucher_code': voucher_code
            }
            event_type = 'loyalty_redemption'
        elif message_type == 'tier_upgrade':
            new_tier = kwargs.get('new_tier', '')
            default_msg = f"Congratulations {name}! You've been upgraded to {new_tier} tier! Enjoy your new benefits."
            context = {
                'name': name,
                'new_tier': new_tier
            }
            event_type = 'loyalty_tier_upgrade'
        elif message_type == 'expiry_warning':
            points = kwargs.get('points', 0)
            days = kwargs.get('days', 30)
            default_msg = f"Hi {name}, {points} loyalty points will expire in {days} days. Redeem them now!"
            context = {
                'name': name,
                'points': points,
                'days': days
            }
            event_type = 'loyalty_expiry_warning'
        else:
            return

        # Try to get custom template from DB, fall back to default
        msg = _get_rendered_message(
            event_type=event_type,
            default_msg=default_msg,
            **context
        )

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


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_router_offline_alert(self, router_name):
    """
    Triggered when a router drops from 'online' to 'offline'.
    Sends an SMS alert to the configured system alert phone number.
    """
    try:
        from .models import SMSNotificationSettings, SMSGatewayConfig, SMSMessage
        from .services.gateway_dispatcher import GatewayDispatcher
        from decimal import Decimal

        settings = SMSNotificationSettings.get_settings()
        
        # Guard: Check if the feature is enabled and a phone number is provided
        if not settings.system_router_offline or not settings.system_alert_phone:
            logger.debug(f"Router offline alert disabled or no alert phone configured. router={router_name}")
            return

        config = SMSGatewayConfig.objects.filter(is_active=True).first()
        if not config:
            logger.warning("No active SMS gateway for offline alert.")
            return

        phone = settings.system_alert_phone
        msg = f"SYSTEM ALERT: Your router '{router_name}' is currently OFFLINE. Please check the network."

        dispatcher = GatewayDispatcher()
        result = dispatcher.send_sms(to=phone, message=msg)

        # Log the system message in the general SMS history
        SMSMessage.objects.create(
            recipient=phone,
            recipient_name="System Admin",
            message=msg,
            status=result.get('status', 'failed'),
            type='automated',
            provider=config.provider,
            provider_message_id=result.get('provider_id', ''),
            cost=result.get('cost', Decimal('0.00')),
            error_message=result.get('error', ''),
        )
        
        if result.get('success'):
            logger.info(f"Router offline alert sent for '{router_name}' to {phone}")
        else:
            logger.warning(f"Router offline alert failed for '{router_name}': {result.get('error')}")
            
    except Exception as e:
        logger.error(f"send_router_offline_alert error for router '{router_name}': {e}")
        raise self.retry(exc=e)


# FIX 5: Campaign bulk SMS — Celery task with tenant context
@shared_task(bind=True, max_retries=2)
def process_campaign_sms(self, campaign_id: int, phones: list, message: str):
    """
    Send bulk campaign SMS and update campaign stats.
    Wrapped with tenant schema context to ensure correct database routing.
    
    Args:
        campaign_id: ID of the SMSCampaign record
        phones: List of phone numbers to send to
        message: SMS content to send
    """
    from apps.messaging.models import SMSCampaign
    from apps.messaging.services.notification_sender import _dispatch
    from django.utils import timezone
    from django_tenants.utils import schema_context, get_public_schema_name
    from apps.core.models import Tenant

    # Find which tenant owns this campaign
    target_schema = None
    with schema_context(get_public_schema_name()):
        for tenant in Tenant.objects.filter(is_active=True).exclude(schema_name='public'):
            with schema_context(tenant.schema_name):
                try:
                    if SMSCampaign.objects.filter(id=campaign_id).exists():
                        target_schema = tenant.schema_name
                        break
                except Exception:
                    continue

    if not target_schema:
        logger.error(f"[CAMPAIGN {campaign_id}] Could not find tenant schema")
        return

    # Execute the campaign in the correct tenant schema
    with schema_context(target_schema):
        try:
            campaign = SMSCampaign.objects.get(id=campaign_id)
        except SMSCampaign.DoesNotExist:
            logger.warning(f"[CAMPAIGN {campaign_id}] Campaign not found in schema {target_schema}")
            return

        sent = 0
        failed = 0

        logger.info(f"[CAMPAIGN {campaign_id}] Starting bulk send to {len(phones)} recipients in {target_schema}")

        for idx, phone in enumerate(phones):
            try:
                ok = _dispatch(phone, message)
                if ok:
                    sent += 1
                else:
                    failed += 1
            except Exception as e:
                logger.error(f"[CAMPAIGN {campaign_id}] Failed to send to {phone}: {e}")
                failed += 1

            # Log progress every 100 messages
            if (idx + 1) % 100 == 0:
                logger.info(
                    f"[CAMPAIGN {campaign_id}] Progress: {idx + 1}/{len(phones)} "
                    f"(sent={sent}, failed={failed})"
                )

        campaign.delivered_count = sent
        campaign.failed_count = failed
        campaign.status = 'completed'
        campaign.completed_at = timezone.now()
        campaign.save(update_fields=['delivered_count', 'failed_count', 'status', 'completed_at'])

        logger.info(
            f"[CAMPAIGN {campaign_id}] Completed: Sent={sent}, Failed={failed}, "
            f"Total={len(phones)}"
        )