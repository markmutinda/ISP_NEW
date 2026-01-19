from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from apps.notifications.models import Notification, AlertRule, NotificationTemplate
from apps.notifications.services import NotificationManager
import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Send scheduled notifications and process alerts'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--limit',
            type=int,
            default=100,
            help='Maximum number of notifications to process'
        )
        parser.add_argument(
            '--alert-check',
            action='store_true',
            help='Check and process alert rules'
        )
        parser.add_argument(
            '--retry-failed',
            action='store_true',
            help='Retry failed notifications'
        )
    
    def handle(self, *args, **options):
        limit = options['limit']
        check_alerts = options['alert_check']
        retry_failed = options['retry_failed']
        
        notification_manager = NotificationManager()
        
        # Send pending notifications
        self.stdout.write("Processing pending notifications...")
        sent_count = self._send_pending_notifications(notification_manager, limit)
        
        # Retry failed notifications
        if retry_failed:
            self.stdout.write("Retrying failed notifications...")
            retry_count = self._retry_failed_notifications(notification_manager, limit)
        
        # Check alert rules
        if check_alerts:
            self.stdout.write("Checking alert rules...")
            alert_count = self._check_alert_rules(notification_manager)
        
        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully sent {sent_count} notifications'
            )
        )
    
    def _send_pending_notifications(self, manager, limit):
        """Send pending notifications"""
        sent_count = 0
        
        # Get pending notifications
        pending_notifications = Notification.objects.filter(
            status='pending',
            sent_at__isnull=True
        ).order_by('-priority', 'created_at')[:limit]
        
        for notification in pending_notifications:
            try:
                success = manager.send_notification(notification)
                if success:
                    sent_count += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'Sent notification {notification.id} to {notification.recipient_email or notification.recipient_phone}'
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING(
                            f'Failed to send notification {notification.id}'
                        )
                    )
            except Exception as e:
                logger.error(f"Error sending notification {notification.id}: {str(e)}")
        
        return sent_count
    
    def _retry_failed_notifications(self, manager, limit):
        """Retry failed notifications"""
        retry_count = 0
        
        # Get failed notifications that can be retried
        failed_notifications = Notification.objects.filter(
            status='failed',
            retry_count__lt=models.F('max_retries'),
            next_retry_at__lte=timezone.now()
        ).order_by('next_retry_at')[:limit]
        
        for notification in failed_notifications:
            try:
                success = manager.send_notification(notification)
                if success:
                    retry_count += 1
                    notification.retry_count += 1
                    notification.save()
                    
                    self.stdout.write(
                        self.style.SUCCESS(
                            f'Retry successful for notification {notification.id}'
                        )
                    )
                else:
                    notification.retry_count += 1
                    notification.next_retry_at = timezone.now() + timedelta(minutes=30)
                    notification.save()
                    
                    self.stdout.write(
                        self.style.WARNING(
                            f'Retry failed for notification {notification.id}'
                        )
                    )
            except Exception as e:
                logger.error(f"Error retrying notification {notification.id}: {str(e)}")
        
        return retry_count
    
    def _check_alert_rules(self, manager):
        """Check and process alert rules"""
        from apps.notifications.models import AlertRule
        from django.apps import apps
        
        alert_count = 0
        now = timezone.now()
        
        # Get active alert rules that need checking
        alert_rules = AlertRule.objects.filter(
            is_active=True,
            last_checked__lt=now - timedelta(minutes=models.F('check_interval'))
        )
        
        for rule in alert_rules:
            try:
                # Check if rule should run based on time
                if not rule.is_time_valid():
                    continue
                
                # Get model class
                try:
                    app_label, model_name = rule.model_name.split('.')
                    model_class = apps.get_model(app_label, model_name)
                except Exception as e:
                    logger.error(f"Invalid model {rule.model_name}: {str(e)}")
                    continue
                
                # Build filter based on condition
                filter_kwargs = self._build_filter(rule)
                if not filter_kwargs:
                    continue
                
                # Query for matching records
                matching_objects = model_class.objects.filter(**filter_kwargs)
                
                if matching_objects.exists():
                    # Rule triggered
                    self.stdout.write(
                        self.style.WARNING(
                            f'Alert rule "{rule.name}" triggered: {matching_objects.count()} matches'
                        )
                    )
                    
                    # Send notifications
                    for obj in matching_objects:
                        manager.trigger_alert(rule, obj)
                        alert_count += 1
                
                # Update last checked time
                rule.last_checked = now
                rule.save()
                
            except Exception as e:
                logger.error(f"Error checking alert rule {rule.id}: {str(e)}")
        
        return alert_count
    
    def _build_filter(self, rule):
        """Build Django filter from alert rule condition"""
        filter_kwargs = {}
        field_lookup = f'{rule.field_name}'
        
        if rule.condition_type == 'greater_than':
            filter_kwargs[f'{field_lookup}__gt'] = rule.condition_value
        elif rule.condition_type == 'less_than':
            filter_kwargs[f'{field_lookup}__lt'] = rule.condition_value
        elif rule.condition_type == 'equals':
            filter_kwargs[field_lookup] = rule.condition_value
        elif rule.condition_type == 'not_equals':
            filter_kwargs[f'{field_lookup}__ne'] = rule.condition_value
        elif rule.condition_type == 'contains':
            filter_kwargs[f'{field_lookup}__icontains'] = rule.condition_value
        elif rule.condition_type == 'starts_with':
            filter_kwargs[f'{field_lookup}__istartswith'] = rule.condition_value
        elif rule.condition_type == 'ends_with':
            filter_kwargs[f'{field_lookup}__iendswith'] = rule.condition_value
        else:
            return None
        
        return filter_kwargs
