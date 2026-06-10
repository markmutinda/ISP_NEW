from celery import shared_task
from django.utils import timezone
import logging
from .services import NotificationManager
from .models import Notification, BulkNotification, AlertRule
from django.db import models
from django.db import ProgrammingError

logger = logging.getLogger(__name__)

@shared_task
def send_notification_task(notification_id):
    """Background task to send a notification"""
    try:
        notification = Notification.objects.get(id=notification_id)
        manager = NotificationManager()
        manager.send_notification(notification)
        return f"Notification {notification_id} sent"
    except Notification.DoesNotExist:
        return f"Notification {notification_id} not found"
    except Exception as e:
        logger.error(f"Error sending notification {notification_id}: {str(e)}")
        raise

@shared_task
def send_bulk_notification_task(bulk_notification_id):
    """Background task to send bulk notifications"""
    try:
        bulk_notification = BulkNotification.objects.get(id=bulk_notification_id)
        manager = NotificationManager()
        result = manager.send_bulk_notification(bulk_notification_id)
        return result
    except BulkNotification.DoesNotExist:
        return {"error": f"Bulk notification {bulk_notification_id} not found"}
    except Exception as e:
        logger.error(f"Error sending bulk notification: {str(e)}")
        raise

@shared_task
def process_alert_rules_task():
    """
    Background task to process alert rules across all active tenants.
    
    This task iterates through every active tenant (ISP) and processes
    their alert rules in isolation. This prevents cross-tenant data leakage
    and handles missing tables gracefully.
    """
    from apps.core.models import Tenant
    from django_tenants.utils import schema_context
    from django.db import connection
    
    results = {
        'total_tenants': 0,
        'processed_tenants': 0,
        'failed_tenants': 0,
        'tenant_results': {},
        'total_processed_rules': 0,
        'total_triggered_alerts': 0
    }
    
    try:
        # Get all active ISPs from the public schema (exclude public schema itself)
        with schema_context('public'):
            tenants = Tenant.objects.filter(
                is_active=True
            ).exclude(
                schema_name='public'
            ).values_list('schema_name', flat=True)
        
        results['total_tenants'] = len(tenants)
        logger.info(f"Processing alert rules for {len(tenants)} tenants: {list(tenants)}")
        
        # Process each tenant's alert rules
        for schema_name in tenants:
            tenant_result = {
                'processed_rules': 0,
                'triggered_alerts': 0,
                'error': None,
                'skipped': False
            }
            
            try:
                # Switch context to the tenant's schema
                with schema_context(schema_name):
                    # Check if the notifications app is migrated for this tenant
                    from django.apps import apps
                    if not apps.is_installed('apps.notifications'):
                        tenant_result['skipped'] = True
                        tenant_result['error'] = "Notifications app not installed"
                        results['failed_tenants'] += 1
                        results['tenant_results'][schema_name] = tenant_result
                        continue
                    
                    # Try to query alert rules - handle case where table doesn't exist
                    try:
                        alert_rules = AlertRule.objects.filter(is_active=True)
                        
                        if not alert_rules.exists():
                            logger.info(f"No active alert rules found for tenant {schema_name}")
                            tenant_result['skipped'] = True
                            tenant_result['error'] = "No active rules"
                            results['processed_tenants'] += 1
                            results['tenant_results'][schema_name] = tenant_result
                            continue
                        
                        manager = NotificationManager()
                        processed = 0
                        triggered = 0
                        
                        for rule in alert_rules:
                            try:
                                # Check if rule should run based on time
                                if not rule.is_time_valid():
                                    continue
                                
                                # Check if it's time to run based on check_interval
                                if rule.last_checked:
                                    next_check = rule.last_checked + timezone.timedelta(minutes=rule.check_interval)
                                    if timezone.now() < next_check:
                                        continue
                                
                                # Test the rule
                                rule_triggered = manager.test_alert_rule(rule)
                                if rule_triggered:
                                    triggered += 1
                                    # Log the triggered rule
                                    logger.info(f"Alert rule '{rule.name}' triggered for tenant {schema_name}")
                                
                                rule.last_checked = timezone.now()
                                rule.save(update_fields=['last_checked'])
                                processed += 1
                                
                            except Exception as rule_error:
                                logger.error(f"Error processing rule {rule.id} for tenant {schema_name}: {str(rule_error)}")
                                continue
                        
                        tenant_result['processed_rules'] = processed
                        tenant_result['triggered_alerts'] = triggered
                        results['total_processed_rules'] += processed
                        results['total_triggered_alerts'] += triggered
                        results['processed_tenants'] += 1
                        
                        if processed > 0:
                            logger.info(f"Processed {processed} rules for tenant {schema_name}, triggered {triggered} alerts")
                        
                    except ProgrammingError as e:
                        # Table doesn't exist in this tenant yet
                        if 'relation' in str(e).lower() and 'does not exist' in str(e).lower():
                            logger.warning(f"AlertRule table doesn't exist for tenant {schema_name}. Run migrations for this tenant.")
                            tenant_result['skipped'] = True
                            tenant_result['error'] = "Table doesn't exist - run migrations"
                            results['failed_tenants'] += 1
                        else:
                            raise
                            
                results['tenant_results'][schema_name] = tenant_result
                
            except Exception as tenant_error:
                logger.error(f"Error processing alert rules for tenant {schema_name}: {str(tenant_error)}", exc_info=True)
                tenant_result['error'] = str(tenant_error)
                results['failed_tenants'] += 1
                results['tenant_results'][schema_name] = tenant_result
        
        logger.info(f"Alert rules processing complete: {results['processed_tenants']} tenants processed, "
                   f"{results['failed_tenants']} failed, "
                   f"{results['total_processed_rules']} rules processed, "
                   f"{results['total_triggered_alerts']} alerts triggered")
        
        return results
        
    except Exception as e:
        logger.error(f"Error in process_alert_rules_task: {str(e)}", exc_info=True)
        results['global_error'] = str(e)
        return results


@shared_task
def retry_failed_notifications_task():
    """
    Finds notifications that failed and attempts to resend them.
    Loops through all tenants to find failures in each schema.
    """
    from django_tenants.utils import schema_context, get_tenant_model
    from .services import NotificationManager
    from .models import Notification
    
    TenantModel = get_tenant_model()
    # Get all tenants except the public schema
    tenants = TenantModel.objects.exclude(schema_name='public')
    
    total_retried = 0
    total_successful = 0
    
    for tenant in tenants:
        with schema_context(tenant.schema_name):
            try:
                manager = NotificationManager()
                failed_notifications = Notification.objects.filter(
                    status='failed',
                    retry_count__lt=models.F('max_retries'),
                    next_retry_at__lte=timezone.now()
                )
                
                retried = 0
                successful = 0
                
                for notification in failed_notifications:
                    try:
                        success = manager.send_notification(notification)
                        if success:
                            successful += 1
                        retried += 1
                    except Exception as e:
                        logger.error(f"Error retrying notification {notification.id} in {tenant.schema_name}: {str(e)}")
                        retried += 1
                
                total_retried += retried
                total_successful += successful
                
                if retried > 0:
                    logger.info(f"Retried {retried} notifications in {tenant.schema_name}, {successful} successful")
                    
            except Exception as e:
                logger.error(f"Error retrying failed notifications in {tenant.schema_name}: {str(e)}")
    
    return {
        'retried': total_retried,
        'successful': total_successful
    }


@shared_task
def clean_old_notifications_task(days_old=90):
    """
    Background task to clean old notifications across all tenants.
    Loops through all tenants and deletes old notifications in each schema.
    """
    from django_tenants.utils import schema_context, get_tenant_model
    from django.utils import timezone
    from datetime import timedelta
    from .models import Notification
    
    TenantModel = get_tenant_model()
    # Get all tenants except the public schema
    tenants = TenantModel.objects.exclude(schema_name='public')
    
    cutoff_date = timezone.now() - timedelta(days=days_old)
    total_deleted = 0
    
    for tenant in tenants:
        with schema_context(tenant.schema_name):
            try:
                # Delete old notifications (keep failed for debugging)
                deleted_count, _ = Notification.objects.filter(
                    created_at__lt=cutoff_date,
                    status__in=['sent', 'delivered', 'read']
                ).delete()
                
                total_deleted += deleted_count
                
                if deleted_count > 0:
                    logger.info(f"Deleted {deleted_count} old notifications in {tenant.schema_name}")
                    
            except Exception as e:
                logger.error(f"Error cleaning old notifications in {tenant.schema_name}: {str(e)}")
    
    logger.info(f"Total notifications deleted across all tenants: {total_deleted}")
    
    return {
        'deleted_notifications': total_deleted,
        'cutoff_date': cutoff_date,
        'days_old': days_old
    }


@shared_task
def process_all_tenant_tasks():
    """
    Master task that processes tenant-specific tasks across all tenants.
    This can be used as a parent task to orchestrate other tenant-specific tasks.
    """
    from apps.core.models import Tenant
    from django_tenants.utils import schema_context
    
    results = {}
    
    try:
        with schema_context('public'):
            tenants = Tenant.objects.filter(is_active=True).exclude(schema_name='public')
        
        for tenant in tenants:
            results[tenant.schema_name] = {
                'alert_rules': None,
                'notifications': None
            }
            
            # Process alert rules for this tenant
            try:
                alert_result = process_alert_rules_task_for_tenant(tenant.schema_name)
                results[tenant.schema_name]['alert_rules'] = alert_result
            except Exception as e:
                logger.error(f"Error processing alert rules for {tenant.schema_name}: {e}")
                results[tenant.schema_name]['alert_rules'] = {'error': str(e)}
            
            # Process other tenant-specific tasks here
        
        return results
        
    except Exception as e:
        logger.error(f"Error in master task: {str(e)}", exc_info=True)
        return {'error': str(e)}


def process_alert_rules_task_for_tenant(schema_name):
    """
    Helper function to process alert rules for a specific tenant.
    This can be called directly or from the master task.
    """
    from django_tenants.utils import schema_context
    from .services import NotificationManager
    
    with schema_context(schema_name):
        try:
            manager = NotificationManager()
            alert_rules = AlertRule.objects.filter(is_active=True)
            
            processed = 0
            triggered = 0
            
            for rule in alert_rules:
                # Check if rule should run based on time
                if not rule.is_time_valid():
                    continue
                
                # Check if it's time to run based on check_interval
                if rule.last_checked:
                    next_check = rule.last_checked + timezone.timedelta(minutes=rule.check_interval)
                    if timezone.now() < next_check:
                        continue
                
                # Test the rule
                rule_triggered = manager.test_alert_rule(rule)
                if rule_triggered:
                    triggered += 1
                
                rule.last_checked = timezone.now()
                rule.save()
                processed += 1
            
            return {
                'processed': processed,
                'triggered': triggered,
                'schema': schema_name
            }
            
        except ProgrammingError as e:
            if 'relation' in str(e).lower() and 'does not exist' in str(e).lower():
                logger.warning(f"AlertRule table doesn't exist for tenant {schema_name}")
                return {
                    'error': 'Table does not exist - run migrations',
                    'schema': schema_name
                }
            raise
        except Exception as e:
            logger.error(f"Error processing tenant {schema_name}: {str(e)}")
            return {
                'error': str(e),
                'schema': schema_name
            }


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM LEAD ALERT TASK
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_telegram_lead_alert(self, name: str, email: str, phone: str, company: str, referral_name: str = "Not specified"):
    """
    Send a Telegram alert to the superadmin group when a new ISP lead submits the contact form.
    
    This runs asynchronously in the background so the lead form submission doesn't have
    to wait for the Telegram API to respond.
    
    Args:
        name: Lead's full name
        email: Lead's email address
        phone: Lead's phone number
        company: Lead's ISP/company name
        referral_name: Optional person or company who referred the lead
    """
    from apps.notifications.services.telegram_service import TelegramService
    
    try:
        # Format a nice looking message using basic HTML tags
        message = (
            f"🚨 <b>NEW ISP LEAD ALERT</b> 🚨\n\n"
            f"👤 <b>Name:</b> {name}\n"
            f"🏢 <b>Company:</b> {company}\n"
            f"📞 <b>Phone:</b> {phone}\n"
            f"✉️ <b>Email:</b> {email}\n\n"
            f"Referred By: {referral_name}\n\n"
            f"<i>Log in to the Netily Superadmin dashboard to view details.</i>"
        )
        
        service = TelegramService()
        success = service.send_message_to_admins(message)
        
        if success:
            logger.info(f"Telegram lead alert sent successfully for lead: {name} ({email})")
        else:
            logger.warning(f"Telegram lead alert failed for lead: {name} ({email})")
            # Don't retry on failure — lead is already saved in the system
            # This prevents duplicate notifications if the API is temporarily down
            
        return {'success': success, 'lead': name, 'email': email}
        
    except Exception as e:
        logger.error(f"Error sending Telegram lead alert for {name} ({email}): {str(e)}")
        # Only retry on connection/network errors, not on validation errors
        if 'ConnectionError' in str(e) or 'timeout' in str(e).lower():
            raise self.retry(exc=e)
        # Otherwise, return failure without retry
        return {'success': False, 'error': str(e), 'lead': name, 'email': email}
