"""
Automated SMS trigger tasks.
Each task checks the tenant's SMSGatewayConfig for the relevant
auto_* flag before sending.
"""
import logging
from celery import shared_task
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta

logger = logging.getLogger(__name__)


def _send_auto_sms(phone: str, message: str, trigger_flag: str, msg_type='automated', schema_name: str = None):
    """
    Internal helper: send an SMS only if the active gateway has the trigger enabled.
    Returns True if sent, False if skipped or failed.
    
    Args:
        phone: Recipient phone number
        message: SMS content
        trigger_flag: Name of the flag to check on SMSGatewayConfig
        msg_type: Type of message ('automated' or other)
        schema_name: Explicit tenant schema name (required for Celery tasks)
    """
    from django_tenants.utils import schema_context
    from .models import SMSGatewayConfig, SMSMessage
    from .services.gateway_dispatcher import GatewayDispatcher

    # Determine schema: explicit > current connection (for HTTP requests)
    _schema = schema_name
    if not _schema:
        from django.db import connection
        _schema = getattr(connection, 'schema_name', None)

    if not _schema or _schema == 'public':
        logger.warning("_send_auto_sms called without valid schema — skipping")
        return False

    with schema_context(_schema):
        config = SMSGatewayConfig.objects.filter(is_active=True).first()
        if not config:
            logger.debug("No active SMS gateway — skipping auto SMS")
            return False

        if not getattr(config, trigger_flag, False):
            logger.debug(f"Trigger {trigger_flag} is disabled — skipping")
            return False

        try:
            # CRITICAL: Pass schema_name explicitly to GatewayDispatcher
            dispatcher = GatewayDispatcher(schema_name=_schema)
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
def send_payment_confirmation_sms(self, customer_id, amount, reference='', schema_name=None):
    """
    Triggered after a payment is marked COMPLETED.
    Delegates to SMSNotifier which respects the PPPoE payment toggle.
    
    Args:
        customer_id: ID of the customer
        amount: Payment amount
        reference: Payment reference number
        schema_name: Tenant schema name (required for multi-tenant)
    """
    from django_tenants.utils import schema_context
    
    if not schema_name:
        logger.error("No schema_name provided to send_payment_confirmation_sms")
        return
    
    with schema_context(schema_name):
        try:
            from apps.customers.models import Customer
            from apps.messaging.services.notification_sender import SMSNotifier
            customer = Customer.objects.select_related('user').get(id=customer_id)
            SMSNotifier.pppoe_payment(
                customer=customer,
                amount=float(amount),
                reference=reference or '',
            )
        except Exception as e:
            logger.error(f"payment_confirmation_sms error: {e}")
            raise self.retry(exc=e)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_welcome_sms(self, customer_id, schema_name=None):
    """
    Triggered when a new customer is created.
    Uses SMSNotifier which respects the PPPoE welcome toggle.
    
    Args:
        customer_id: ID of the customer
        schema_name: Tenant schema name (required for multi-tenant)
    """
    from django_tenants.utils import schema_context
    
    if not schema_name:
        logger.error("No schema_name provided to send_welcome_sms")
        return
    
    with schema_context(schema_name):
        try:
            from apps.customers.models import Customer
            from apps.messaging.services.notification_sender import SMSNotifier
            customer = Customer.objects.select_related('user').get(id=customer_id)
            # Delegate entirely to SMSNotifier which checks the new toggle
            username = ''
            password = ''
            try:
                creds = customer.radius_credentials
                username = creds.username or ''
                password = creds.password or ''
            except Exception:
                pass
            SMSNotifier.pppoe_welcome(customer=customer, username=username, password=password)
        except Exception as e:
            logger.error(f"welcome_sms error: {e}")
            raise self.retry(exc=e)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_expiry_reminder_sms(self, customer_id, days_left=2, schema_name=None):
    """
    Triggered before a subscription/plan expires.
    Delegates to SMSNotifier which respects the PPPoE expiry reminder toggle.
    
    Args:
        customer_id: ID of the customer
        days_left: Number of days until expiry
        schema_name: Tenant schema name (required for multi-tenant)
    """
    from django_tenants.utils import schema_context
    
    if not schema_name:
        logger.error("No schema_name provided to send_expiry_reminder_sms")
        return
    
    with schema_context(schema_name):
        try:
            from apps.customers.models import Customer
            from apps.messaging.services.notification_sender import SMSNotifier
            customer = Customer.objects.select_related('user').get(id=customer_id)
            plan_name = ''
            try:
                service = customer.services.filter(status='ACTIVE', plan__isnull=False).first()
                if service and service.plan:
                    plan_name = service.plan.name or ''
            except Exception:
                pass
            SMSNotifier.pppoe_expiry_reminder(
                customer=customer,
                days_left=days_left,
                plan_name=plan_name,
            )
        except Exception as e:
            logger.error(f"expiry_reminder_sms error: {e}")
            raise self.retry(exc=e)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_service_suspension_sms(self, customer_id, reason='', schema_name=None):
    """
    Triggered when a customer's service is suspended.
    Uses SMSNotifier which respects the PPPoE suspension toggle.
    
    Args:
        customer_id: ID of the customer
        reason: Reason for suspension
        schema_name: Tenant schema name (required for multi-tenant)
    """
    from django_tenants.utils import schema_context
    
    if not schema_name:
        logger.error("No schema_name provided to send_service_suspension_sms")
        return
    
    with schema_context(schema_name):
        try:
            from apps.customers.models import Customer
            from apps.messaging.services.notification_sender import SMSNotifier
            customer = Customer.objects.select_related('user').get(id=customer_id)
            # Delegate to SMSNotifier which checks the new pppoe_service_suspended toggle
            SMSNotifier.pppoe_suspended(customer=customer, reason=reason)
        except Exception as e:
            logger.error(f"service_suspension_sms error: {e}")
            raise self.retry(exc=e)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_loyalty_notification_sms(self, customer_id, message_type='points_earned', schema_name=None, **kwargs):
    """
    Loyalty program SMS notifications.
    message_type: points_earned | redemption | tier_upgrade | expiry_warning
    Uses dynamic template from DB with event_type='loyalty_{message_type}'
    
    Args:
        customer_id: ID of the customer
        message_type: Type of loyalty notification
        schema_name: Tenant schema name (required for multi-tenant)
        **kwargs: Additional context variables
    """
    from django_tenants.utils import schema_context
    
    if not schema_name:
        logger.error("No schema_name provided to send_loyalty_notification_sms")
        return
    
    with schema_context(schema_name):
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

            # Use the auto-sms helper with explicit schema
            _send_auto_sms(
                phone=phone, 
                message=msg, 
                trigger_flag='auto_loyalty', 
                msg_type='automated',
                schema_name=schema_name
            )
        except Exception as e:
            logger.error(f"loyalty_notification_sms error: {e}")
            raise self.retry(exc=e)


# ─────────────────────────────────────────────────────────────────────────────
# HOTSPOT TASKS
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name='apps.billing.tasks.send_hotspot_expiry_warnings')
def send_hotspot_expiry_warnings():
    """Deprecated — expiry warning toggle removed. No-op."""
    return {'warned': 0}


def _for_each_tenant(func):
    """
    Helper to run a function for each tenant schema.
    Returns aggregated results.
    """
    from django_tenants.utils import schema_context, get_tenant_model
    TenantModel = get_tenant_model()
    results = {}
    
    for tenant in TenantModel.objects.exclude(schema_name='public'):
        with schema_context(tenant.schema_name):
            try:
                result = func(tenant)
                results[tenant.schema_name] = result
            except Exception as e:
                logger.error(f"Error in tenant {tenant.schema_name}: {e}")
                results[tenant.schema_name] = {'error': str(e)}
    
    return results


@shared_task(name='apps.billing.tasks.notify_expired_hotspot_sessions')
def notify_expired_hotspot_sessions():
    """
    Send 'session expired' SMS after marking sessions expired.
    Runs every 5 minutes.
    """
    def _notify(tenant):
        from apps.billing.models.hotspot_models import HotspotSession
        from apps.messaging.services.notification_sender import SMSNotifier

        now = timezone.now()
        recently_expired = HotspotSession.objects.filter(
            status='expired',
            expires_at__gte=now - timedelta(minutes=10),
            expires_at__lte=now,
        )
        count = 0
        for session in recently_expired:
            try:
                SMSNotifier.hotspot_session_expired(session)
                count += 1
            except Exception as e:
                logger.warning(f"Expired SMS failed for {session.session_id}: {e}")
        return {'notified': count}

    try:
        return _for_each_tenant(_notify)
    except Exception as e:
        logger.error(f"Notify expired sessions task failed: {e}", exc_info=True)
        return {'error': str(e)}


def _dispatch_router_offline_sms(router_name: str, schema_name: str = None):
    """
    Inner function that runs inside the correct tenant schema context.
    Reads notification settings, validates recipients, and sends SMS.
    Supports both legacy single-number and new multi-number configurations.
    
    Args:
        router_name: Name of the offline router
        schema_name: Explicit tenant schema name
    """
    from .models import SMSNotificationSettings, SMSGatewayConfig, SMSMessage
    from .services.gateway_dispatcher import GatewayDispatcher
    from decimal import Decimal
    from django.db import connection

    # Determine schema
    _schema = schema_name or getattr(connection, 'schema_name', None)
    
    if not _schema or _schema == 'public':
        logger.warning(f"[ROUTER ALERT] No valid schema for router '{router_name}'")
        return

    logger.debug(f"[ROUTER ALERT] _dispatch_router_offline_sms() called for router: {router_name} (schema: {_schema})")

    try:
        settings_obj = SMSNotificationSettings.objects.first()
    except Exception as e:
        logger.warning(f"[ROUTER ALERT] Could not read SMSNotificationSettings: {e}")
        return

    if not settings_obj:
        logger.info(f"[ROUTER ALERT] No SMSNotificationSettings record found for this tenant — skipping '{router_name}'")
        return

    # FIXED: Use 'or' operator to check both possible field names
    is_enabled = getattr(settings_obj, 'router_offline_enabled', False) or getattr(settings_obj, 'system_router_offline', False)
    logger.debug(f"[ROUTER ALERT] Toggle Status in Database: {is_enabled}")

    if not is_enabled:
        logger.info(f"[ROUTER ALERT] Alerts are currently toggled OFF in settings — skipping for '{router_name}'")
        return

    # SAFE CHECK: Get the list of numbers from JSON or fallback to the single string field
    numbers = list(getattr(settings_obj, 'router_offline_numbers', []) or [])
    logger.debug(f"[ROUTER ALERT] router_offline_numbers (JSON array): {numbers}")
    
    # FALLBACK: Check legacy single phone field
    if not numbers:
        legacy_phone = getattr(settings_obj, 'system_alert_phone', '')
        logger.debug(f"[ROUTER ALERT] system_alert_phone (legacy field): '{legacy_phone}'")
        if legacy_phone:
            numbers = [legacy_phone]
            logger.info(f"[ROUTER ALERT] Using legacy system_alert_phone: {legacy_phone}")
        else:
            logger.info(f"[ROUTER ALERT] Router '{router_name}' offline but NO PHONE NUMBERS are configured")
            return
            
    logger.debug(f"[ROUTER ALERT] Sending to {len(numbers)} number(s): {numbers}")

    # Build the alert message
    message = (
        f"⚠️ ALERT: Router '{router_name}' has gone OFFLINE. "
        f"Please check your network immediately."
    )
    logger.debug(f"[ROUTER ALERT] Message: {message}")

    # Get the active gateway
    config = SMSGatewayConfig.objects.filter(is_active=True).first()
    use_inbuilt = getattr(settings_obj, 'use_inbuilt_system', False)

    if not config and not use_inbuilt:
        logger.warning(f"[ROUTER ALERT] No active SMS gateway found to send alert for '{router_name}'")
        return

    logger.debug("[ROUTER ALERT] Gateway OK. Dispatching SMS...")

    sent_count = 0
    failed_phones = []

    for phone in numbers:
        if not phone or not phone.strip():
            logger.debug("[ROUTER ALERT] Skipping empty phone number")
            continue
            
        phone = phone.strip()
        logger.debug(f"[ROUTER ALERT] Sending to: {phone}")
        try:
            # CRITICAL: Pass schema_name explicitly to GatewayDispatcher
            dispatcher = GatewayDispatcher(schema_name=_schema)

            # Deduct from wallet if using inbuilt system (handled inside send_sms now)
            result = dispatcher.send_sms(to=phone, message=message)
            logger.debug(f"[ROUTER ALERT] Gateway result: success={result.get('success')}, error={result.get('error')}")

            # Log the system message in the general SMS history
            SMSMessage.objects.create(
                recipient=phone,
                recipient_name="System Admin",
                message=message,
                status=result.get('status', 'failed'),
                type='automated',
                provider=config.provider if config else 'inbuilt',
                provider_message_id=result.get('provider_id', ''),
                cost=result.get('cost', Decimal('0.00')),
                error_message=result.get('error', ''),
            )
            
            if result.get('success'):
                sent_count += 1
                logger.info(f"[ROUTER ALERT] ✅ Sent to {phone} for router '{router_name}'")
            else:
                failed_phones.append(phone)
                logger.warning(f"[ROUTER ALERT] ❌ Failed to send to {phone}: {result.get('error')}")
                
        except Exception as e:
            failed_phones.append(phone)
            logger.error(f"[ROUTER ALERT] Exception sending to {phone}: {e}")

    logger.info(
        f"[ROUTER ALERT] Completed — router='{router_name}' "
        f"sent={sent_count}/{len(numbers)}"
    )
    
    if failed_phones:
        logger.warning(
            f"[ROUTER ALERT] Failed recipients for '{router_name}': {failed_phones}"
        )


@shared_task(
    bind=True,
    queue='notifications',
    max_retries=3,
    default_retry_delay=30,
    ignore_result=True,
)
def send_router_offline_alert(self, router_name: str, schema_name: str = None):
    """
    Send SMS to configured alert numbers when a MikroTik router goes offline.

    Called from Router.sync_status() on the online → offline transition.
    Runs inside the correct tenant schema so it reads the right settings.
    
    Supports multiple recipient numbers via the new router_offline_numbers JSONField.
    """
    from django_tenants.utils import schema_context
    
    logger.info(f"[ROUTER ALERT] Offline task triggered for router: {router_name} (schema: {schema_name})")
    
    if not schema_name:
        logger.error("No schema_name provided to send_router_offline_alert")
        return
    
    try:
        with schema_context(schema_name):
            _dispatch_router_offline_sms(router_name, schema_name=schema_name)
    except Exception as exc:
        logger.error(
            f"[ROUTER ALERT] Failed for router '{router_name}' "
            f"(schema={schema_name}): {exc}",
            exc_info=True,
        )
        raise self.retry(exc=exc)


def _dispatch_router_online_sms(router_name: str, schema_name: str = None):
    """
    Inner function that runs inside the correct tenant schema context.
    Reads notification settings, validates recipients, and sends SMS.
    Reuses the same numbers and toggle as offline alerts.
    
    Args:
        router_name: Name of the online router
        schema_name: Explicit tenant schema name
    """
    from .models import SMSNotificationSettings, SMSGatewayConfig, SMSMessage
    from .services.gateway_dispatcher import GatewayDispatcher
    from decimal import Decimal
    from django.db import connection

    # Determine schema
    _schema = schema_name or getattr(connection, 'schema_name', None)
    
    if not _schema or _schema == 'public':
        logger.warning(f"[ROUTER ONLINE ALERT] No valid schema for router '{router_name}'")
        return

    logger.debug(f"[ROUTER ONLINE ALERT] _dispatch_router_online_sms() called for router: {router_name} (schema: {_schema})")

    try:
        settings_obj = SMSNotificationSettings.objects.first()
    except Exception as e:
        logger.warning(f"[ROUTER ONLINE ALERT] Could not read SMSNotificationSettings: {e}")
        return

    if not settings_obj:
        logger.info(f"[ROUTER ONLINE ALERT] No SMSNotificationSettings record found for this tenant — skipping '{router_name}'")
        return

    # Reuse the same toggle as offline alerts
    is_enabled = getattr(settings_obj, 'router_offline_enabled', False) or getattr(settings_obj, 'system_router_offline', False)
    logger.debug(f"[ROUTER ONLINE ALERT] Toggle Status in Database: {is_enabled}")

    if not is_enabled:
        logger.info(f"[ROUTER ONLINE ALERT] Alerts are currently toggled OFF in settings — skipping for '{router_name}'")
        return

    # Get the list of numbers (same as offline alerts)
    numbers = list(getattr(settings_obj, 'router_offline_numbers', []) or [])
    logger.debug(f"[ROUTER ONLINE ALERT] router_offline_numbers (JSON array): {numbers}")
    
    # FALLBACK: Check legacy single phone field
    if not numbers:
        legacy_phone = getattr(settings_obj, 'system_alert_phone', '')
        logger.debug(f"[ROUTER ONLINE ALERT] system_alert_phone (legacy field): '{legacy_phone}'")
        if legacy_phone:
            numbers = [legacy_phone]
            logger.info(f"[ROUTER ONLINE ALERT] Using legacy system_alert_phone: {legacy_phone}")
        else:
            logger.info(f"[ROUTER ONLINE ALERT] Router '{router_name}' online but NO PHONE NUMBERS are configured")
            return
            
    logger.debug(f"[ROUTER ONLINE ALERT] Sending to {len(numbers)} number(s): {numbers}")

    # Build the online alert message
    message = (
        f"✅ RESTORED: Router '{router_name}' is back ONLINE. "
        f"Network connectivity has been restored."
    )
    logger.debug(f"[ROUTER ONLINE ALERT] Message: {message}")

    # Get the active gateway
    config = SMSGatewayConfig.objects.filter(is_active=True).first()
    use_inbuilt = getattr(settings_obj, 'use_inbuilt_system', False)

    if not config and not use_inbuilt:
        logger.warning(f"[ROUTER ONLINE ALERT] No active SMS gateway found to send alert for '{router_name}'")
        return

    logger.debug("[ROUTER ONLINE ALERT] Gateway OK. Dispatching SMS...")

    sent_count = 0
    failed_phones = []

    for phone in numbers:
        if not phone or not phone.strip():
            logger.debug("[ROUTER ONLINE ALERT] Skipping empty phone number")
            continue
            
        phone = phone.strip()
        logger.debug(f"[ROUTER ONLINE ALERT] Sending to: {phone}")
        try:
            # CRITICAL: Pass schema_name explicitly to GatewayDispatcher
            dispatcher = GatewayDispatcher(schema_name=_schema)

            result = dispatcher.send_sms(to=phone, message=message)
            logger.debug(f"[ROUTER ONLINE ALERT] Gateway result: success={result.get('success')}, error={result.get('error')}")

            # Log the system message in the general SMS history
            SMSMessage.objects.create(
                recipient=phone,
                recipient_name="System Admin",
                message=message,
                status=result.get('status', 'failed'),
                type='automated',
                provider=config.provider if config else 'inbuilt',
                provider_message_id=result.get('provider_id', ''),
                cost=result.get('cost', Decimal('0.00')),
                error_message=result.get('error', ''),
            )
            
            if result.get('success'):
                sent_count += 1
                logger.info(f"[ROUTER ONLINE ALERT] ✅ Sent to {phone} for router '{router_name}'")
            else:
                failed_phones.append(phone)
                logger.warning(f"[ROUTER ONLINE ALERT] ❌ Failed to send to {phone}: {result.get('error')}")
                
        except Exception as e:
            failed_phones.append(phone)
            logger.error(f"[ROUTER ONLINE ALERT] Exception sending to {phone}: {e}")

    logger.info(
        f"[ROUTER ONLINE ALERT] Completed — router='{router_name}' "
        f"sent={sent_count}/{len(numbers)}"
    )
    
    if failed_phones:
        logger.warning(
            f"[ROUTER ONLINE ALERT] Failed recipients for '{router_name}': {failed_phones}"
        )


@shared_task(
    bind=True,
    queue='notifications',
    max_retries=3,
    default_retry_delay=30,
    ignore_result=True,
)
def send_router_online_alert(self, router_name: str, schema_name: str = None):
    """
    Send SMS to configured alert numbers when a MikroTik router comes back online.
    
    Called from Router.sync_status() on the offline → online transition.
    Runs inside the correct tenant schema so it reads the right settings.
    
    Supports multiple recipient numbers via the router_offline_numbers JSONField.
    (Reuses the same numbers list since online alerts are paired with offline alerts)
    """
    from django_tenants.utils import schema_context
    
    logger.info(f"[ROUTER ONLINE ALERT] Online task triggered for router: {router_name} (schema: {schema_name})")
    
    if not schema_name:
        logger.error("No schema_name provided to send_router_online_alert")
        return
    
    try:
        with schema_context(schema_name):
            _dispatch_router_online_sms(router_name, schema_name=schema_name)
    except Exception as exc:
        logger.error(
            f"[ROUTER ONLINE ALERT] Failed for router '{router_name}' "
            f"(schema={schema_name}): {exc}",
            exc_info=True,
        )
        raise self.retry(exc=exc)


# FIX 5: Campaign bulk SMS — Celery task with tenant context
@shared_task(bind=True, max_retries=2)
def process_campaign_sms(self, campaign_id: int, phones: list, message: str, schema_name: str = None):
    """
    Send bulk campaign SMS and update campaign stats.

    CRITICAL: schema_name MUST be passed explicitly by the caller (the view
    that created the campaign, while it is still inside the correct
    tenant's own request context). SMSCampaign ids are PER-TENANT
    auto-increment values (messaging is a TENANT_APP — every tenant has
    its own table/sequence), so two different tenants can easily have a
    campaign with the SAME id. Searching across tenants for "the first
    schema that has a campaign with this id" is unsafe — it is exactly
    what caused one tenant's SMS gateway/credentials to be used to send
    another tenant's campaign. We never guess anymore: if schema_name is
    missing, we refuse to run.

    Args:
        campaign_id: ID of the SMSCampaign record (per-tenant, NOT globally unique)
        phones: List of phone numbers to send to
        message: SMS content to send
        schema_name: The tenant schema that owns this campaign (REQUIRED)
    """
    from apps.messaging.models import SMSCampaign
    from django.utils import timezone
    from django_tenants.utils import schema_context
    from .services.gateway_dispatcher import GatewayDispatcher

    if not schema_name or schema_name == 'public':
        logger.error(
            f"[CAMPAIGN {campaign_id}] Refusing to run — no valid schema_name "
            f"was passed to process_campaign_sms. This task must always be "
            f"queued with schema_name=<tenant schema> to avoid cross-tenant "
            f"SMS gateway leakage."
        )
        return

    # Execute the campaign ONLY in the schema we were explicitly told about.
    # No cross-tenant search, ever.
    with schema_context(schema_name):
        try:
            campaign = SMSCampaign.objects.get(id=campaign_id)
        except SMSCampaign.DoesNotExist:
            logger.warning(f"[CAMPAIGN {campaign_id}] Campaign not found in schema {schema_name}")
            return

        sent = 0
        failed = 0

        logger.info(f"[CAMPAIGN {campaign_id}] Starting bulk send to {len(phones)} recipients in {schema_name}")

        for idx, phone in enumerate(phones):
            try:
                # Pass schema_name explicitly — this is what loads the
                # correct tenant's SMSGatewayConfig (inbuilt vs custom keys)
                dispatcher = GatewayDispatcher(schema_name=schema_name)
                result = dispatcher.send_sms(to=phone, message=message)

                if result.get('success', False):
                    sent += 1
                else:
                    failed += 1
                    logger.warning(f"[CAMPAIGN {campaign_id}] Failed to send to {phone}: {result.get('error')}")
            except Exception as e:
                logger.error(f"[CAMPAIGN {campaign_id}] Exception sending to {phone}: {e}")
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


# ─────────────────────────────────────────────────────────────────────────────
# SMS HISTORY CLEANUP TASK
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name='apps.messaging.tasks.cleanup_old_sms_history')
def cleanup_old_sms_history():
    """
    Delete SMS messages older than 90 days to prevent table bloat.
    Runs daily — safe to delete since history is cosmetic only.
    """
    from apps.messaging.models import SMSMessage
    from django_tenants.utils import get_tenant_model, schema_context

    TenantModel = get_tenant_model()
    total_deleted = 0

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                deleted = SMSMessage.cleanup_old_messages(days=90)
                if deleted:
                    logger.info(
                        f"[SMS CLEANUP] Deleted {deleted} old messages "
                        f"from {tenant.schema_name}"
                    )
                total_deleted += deleted
        except Exception as e:
            logger.error(f"[SMS CLEANUP] Error in {tenant.schema_name}: {e}")

    return {'deleted': total_deleted}


# ════════════════════════════════════════════════════════════════════════════
# SMS DEDUPLICATION LOG CLEANUP (TENANT‑AWARE)
# ════════════════════════════════════════════════════════════════════════════

@shared_task(name='apps.messaging.tasks.cleanup_sms_dedup_log')
def cleanup_sms_dedup_log():
    """
    Delete SMS dedup log entries older than 7 days for all tenant schemas.
    """
    from django.utils import timezone
    import datetime
    from django_tenants.utils import schema_context, get_tenant_model
    from apps.messaging.models import SMSDeduplicationLog

    TenantModel = get_tenant_model()
    cutoff = timezone.now() - datetime.timedelta(days=7)
    total_deleted = 0

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                deleted, _ = SMSDeduplicationLog.objects.filter(sent_at__lt=cutoff).delete()
                if deleted:
                    logger.info(f"[SMS DEDUP CLEANUP] Deleted {deleted} entries from {tenant.schema_name}")
                total_deleted += deleted
        except Exception as e:
            logger.error(f"[SMS DEDUP CLEANUP] Error in {tenant.schema_name}: {e}")

    logger.info(f"Cleaned up {total_deleted} old SMS dedup log entries across all tenants")
    return {'deleted': total_deleted}