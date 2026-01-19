from celery import shared_task
from django.utils import timezone
import logging
from .services import NotificationManager
from .models import Notification, BulkNotification, AlertRule

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
    """Background task to process alert rules"""
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
            triggered = manager.test_alert_rule(rule)
            if triggered:
                # Get matching objects and send notifications
                # This would query the database for matching objects
                pass
            
            rule.last_checked = timezone.now()
            rule.save()
            processed += 1
        
        return {
            'processed': processed,
            'triggered': triggered
        }
    except Exception as e:
        logger.error(f"Error processing alert rules: {str(e)}")
        raise

@shared_task
def retry_failed_notifications_task():
    """Background task to retry failed notifications"""
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
            success = manager.send_notification(notification)
            if success:
                successful += 1
            retried += 1
        
        return {
            'retried': retried,
            'successful': successful
        }
    except Exception as e:
        logger.error(f"Error retrying failed notifications: {str(e)}")
        raise

@shared_task
def clean_old_notifications_task(days_old=90):
    """Background task to clean old notifications"""
    try:
        from django.utils import timezone
        from datetime import timedelta
        
        cutoff_date = timezone.now() - timedelta(days=days_old)
        
        # Delete old notifications (keep failed for debugging)
        deleted_count, _ = Notification.objects.filter(
            created_at__lt=cutoff_date,
            status__in=['sent', 'delivered', 'read']
        ).delete()
        
        # Archive or delete old logs
        # Implement based on your needs
        
        return {
            'deleted_notifications': deleted_count,
            'cutoff_date': cutoff_date
        }
    except Exception as e:
        logger.error(f"Error cleaning old notifications: {str(e)}")
        raise
