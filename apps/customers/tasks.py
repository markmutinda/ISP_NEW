import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def delayed_service_activation(self, service_id):
    """
    Auto-activate a PENDING service after a scheduled delay (e.g. 1 hour).
    Called via apply_async(countdown=...) from the service creation serializer.
    """
    from apps.customers.models import ServiceConnection

    try:
        service = ServiceConnection.objects.get(id=service_id)
    except ServiceConnection.DoesNotExist:
        logger.warning("delayed_service_activation: Service %s not found", service_id)
        return

    if service.status != 'PENDING':
        logger.info(
            "delayed_service_activation: Service %s is already %s, skipping",
            service_id, service.status,
        )
        return

    try:
        service.activate_service()
        logger.info("delayed_service_activation: Service %s activated successfully", service_id)
    except Exception as exc:
        logger.error("delayed_service_activation: Failed to activate service %s: %s", service_id, exc)
        raise self.retry(exc=exc)


@shared_task(name='apps.customers.tasks.process_expired_subscriptions')
def process_expired_subscriptions():
    """
    Periodic cron task to monitor and suspend accounts with expired subscriptions.
    Fires every 5 minutes across all isolated tenant schemas.
    """
    from django_tenants.utils import get_tenant_model, schema_context
    from django.utils import timezone
    
    TenantModel = get_tenant_model()
    now = timezone.now()
    stats = {'tenants': 0, 'expired': 0, 'errors': 0}
    tenants = TenantModel.objects.exclude(schema_name='public').values_list('schema_name', flat=True)
    
    for schema_name in tenants:
        try:
            with schema_context(schema_name):
                from apps.billing.models.subscription_models import Subscription
                from apps.radius.models import CustomerRadiusCredentials
                
                expired_subs = Subscription.objects.filter(
                    status='ACTIVE',
                    expires_at__isnull=False,
                    expires_at__lt=now,
                ).select_related('customer', 'customer__user', 'service_connection', 'plan')
                
                for sub in expired_subs:
                    try:
                        customer = sub.customer
                        sub.status = 'EXPIRED'
                        sub.save(update_fields=['status', 'updated_at'])
                        
                        # Terminate RADIUS profile authentication permissions
                        try:
                            creds = CustomerRadiusCredentials.objects.filter(customer=customer).first()
                            if creds and creds.is_enabled:
                                creds.is_enabled = False
                                creds.disabled_reason = 'Subscription expired'
                                creds.save(update_fields=['is_enabled', 'disabled_reason', 'updated_at'])
                                creds.sync_to_radius()
                                
                                # Send dynamic CoA packet to drop active PPPoE interface sessions instantly
                                try:
                                    from apps.radius.services.coa_service import CoAService
                                    router_ip = creds.router.vpn_ip_address or creds.router.ip_address if creds.router else None
                                    if router_ip:
                                        CoAService().disconnect_user(username=creds.username, nas_ip_address=router_ip)
                                        logger.info(f"[EXPIRY] CoA session drop executed for {creds.username} via {router_ip}")
                                except Exception as coa_err:
                                    logger.warning(f"[EXPIRY] CoA disconnect bypassed for {creds.username}: {coa_err}")
                        except Exception as radius_err:
                            logger.error(f"[EXPIRY] RADIUS status update failed for {customer.customer_code}: {radius_err}")

                        # Switch the core connection status record to suspended
                        if sub.service_connection:
                            sub.service_connection.status = 'SUSPENDED'
                            sub.service_connection.save(update_fields=['status'])

                        # Dispatch SMS warning message safely
                        try:
                            from apps.messaging.services.notification_sender import SMSNotifier
                            # Handled defensively without reason keyword argument to prevent signature mismatches
                            SMSNotifier.pppoe_suspended(customer)
                        except Exception as sms_err:
                            logger.warning(f"[EXPIRY] Suspension SMS delivery skipped for {customer.customer_code}: {sms_err}")
                            
                        stats['expired'] += 1
                    except Exception as sub_err:
                        stats['errors'] += 1
                        logger.error(f"[EXPIRY] Individual record parse error in schema {schema_name}: {sub_err}")
            stats['tenants'] += 1
        except Exception as tenant_err:
            stats['errors'] += 1
            logger.error(f"[EXPIRY] Fatal loop error in schema context {schema_name}: {tenant_err}")
            
    return stats