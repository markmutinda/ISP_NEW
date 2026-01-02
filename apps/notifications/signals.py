from django.db.models.signals import post_save, pre_save, post_delete
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from django.utils import timezone
import logging

from .models import (
    Notification, 
    NotificationPreference,
    NotificationTemplate
)
from .services import NotificationManager

logger = logging.getLogger(__name__)
User = get_user_model()

# User signals
@receiver(post_save, sender=User)
def create_user_notification_preferences(sender, instance, created, **kwargs):
    """Create notification preferences when a user is created"""
    if created:
        try:
            NotificationPreference.objects.get_or_create(user=instance)
            logger.info(f"Created notification preferences for user {instance.email}")
        except Exception as e:
            logger.error(f"Error creating notification preferences: {str(e)}")

# Notification signals
@receiver(pre_save, sender=Notification)
def pre_save_notification(sender, instance, **kwargs):
    """Handle notification before saving"""
    if not instance.pk:  # New notification
        # Set default priority if not set
        if not instance.priority:
            instance.priority = 2
        
        # Set retry count
        if instance.status == 'failed' and instance.retry_count == 0:
            instance.retry_count = 1

@receiver(post_save, sender=Notification)
def post_save_notification(sender, instance, created, **kwargs):
    """Handle notification after saving"""
    if created and instance.status == 'pending':
        # Auto-send high priority notifications
        if instance.priority >= 4:  # Urgent or Critical
            try:
                manager = NotificationManager()
                manager.send_notification(instance)
            except Exception as e:
                logger.error(f"Error auto-sending notification {instance.id}: {str(e)}")

# Alert rule signals
@receiver(post_save, sender=NotificationTemplate)
def update_alert_rules_on_template_change(sender, instance, **kwargs):
    """Update alert rules when template is deactivated"""
    if not instance.is_active:
        # Deactivate alert rules using this template
        from .models import AlertRule
        alert_rules = AlertRule.objects.filter(notification_templates=instance)
        for rule in alert_rules:
            if rule.notification_templates.count() == 1:
                # This was the only template, deactivate the rule
                rule.is_active = False
                rule.save()
                logger.info(f"Deactivated alert rule {rule.name} due to template deactivation")

# Add more signals as needed for integration with other apps