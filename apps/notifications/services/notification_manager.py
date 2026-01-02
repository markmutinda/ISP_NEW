import logging
from django.utils import timezone
from django.db import transaction
from django.contrib.contenttypes.models import ContentType
from typing import Dict, List, Optional, Any
from ..models import (
    Notification, 
    NotificationTemplate, 
    AlertRule,
    NotificationPreference
)

logger = logging.getLogger(__name__)

class NotificationManager:
    """Main manager class for handling all notifications"""
    
    def __init__(self):
        from .sms_service import SMSService
        from .email_service import EmailService
        from .push_notifications import PushNotificationService
        
        self.sms_service = SMSService()
        self.email_service = EmailService()
        self.push_service = PushNotificationService()
    
    def send_notification(self, notification: Notification) -> bool:
        """Send a notification"""
        try:
            # Check user preferences
            if notification.user:
                try:
                    preference = NotificationPreference.objects.get(user=notification.user)
                    
                    # Check if user can receive this type of notification
                    if not preference.can_receive_notification(notification.notification_type):
                        notification.mark_as_failed("User has disabled this notification type")
                        return False
                    
                    # Check quiet hours
                    if preference.is_quiet_hours():
                        notification.metadata['quiet_hours'] = True
                        notification.save()
                        # Queue for later or skip based on priority
                        if notification.priority < 3:  # Low/Medium priority
                            notification.status = 'pending'
                            notification.save()
                            return True  # Will be sent later
                
                except NotificationPreference.DoesNotExist:
                    # Use default preferences
                    pass
            
            # Send based on type
            if notification.notification_type == 'sms':
                success = self._send_sms(notification)
            elif notification.notification_type == 'email':
                success = self._send_email(notification)
            elif notification.notification_type == 'push':
                success = self._send_push(notification)
            elif notification.notification_type == 'in_app':
                success = self._send_in_app(notification)
            else:
                notification.mark_as_failed(f"Unknown notification type: {notification.notification_type}")
                return False
            
            return success
            
        except Exception as e:
            logger.error(f"Error sending notification {notification.id}: {str(e)}")
            notification.mark_as_failed(str(e))
            return False
    
    def send_template_notification(
        self,
        trigger_event: str,
        recipient_data: Dict,
        template_variables: Dict,
        priority: int = 2
    ) -> Optional[Notification]:
        """Send notification using a template"""
        try:
            # Get template for trigger event
            template = NotificationTemplate.objects.get(
                trigger_event=trigger_event,
                is_active=True
            )
            
            # Apply template variables
            message = template.message_template
            for key, value in template_variables.items():
                placeholder = f'{{{{{key}}}}}'
                message = message.replace(placeholder, str(value))
            
            # Determine recipient
            user = recipient_data.get('user')
            email = recipient_data.get('email')
            phone = recipient_data.get('phone')
            
            # Create notification
            notification = Notification.objects.create(
                user=user,
                template=template,
                notification_type=template.notification_type,
                subject=template.subject,
                message=message,
                recipient_email=email,
                recipient_phone=phone,
                priority=priority,
                metadata={
                    'trigger_event': trigger_event,
                    'template_variables': template_variables,
                    'template_id': template.id
                }
            )
            
            # Send notification
            self.send_notification(notification)
            
            return notification
            
        except NotificationTemplate.DoesNotExist:
            logger.warning(f"No template found for trigger event: {trigger_event}")
            return None
        except Exception as e:
            logger.error(f"Error sending template notification: {str(e)}")
            return None
    
    def trigger_alert(self, alert_rule: AlertRule, triggered_object: Any) -> List[Notification]:
        """Trigger an alert rule for a specific object"""
        notifications = []
        
        try:
            # Check cooldown
            if alert_rule.last_triggered:
                cooldown_end = alert_rule.last_triggered + timezone.timedelta(
                    minutes=alert_rule.cooldown_minutes
                )
                if timezone.now() < cooldown_end:
                    return notifications
            
            # Get target users
            users = self._get_alert_recipients(alert_rule)
            
            # Send notifications for each template
            for template in alert_rule.notification_templates.all():
                # Prepare template variables from object
                template_variables = self._extract_variables(triggered_object)
                
                for user in users:
                    notification = self.send_template_notification(
                        trigger_event=template.trigger_event,
                        recipient_data={'user': user},
                        template_variables=template_variables,
                        priority=template.priority
                    )
                    
                    if notification:
                        notification.metadata['alert_rule_id'] = alert_rule.id
                        notification.metadata['triggered_object_id'] = triggered_object.id
                        notification.metadata['triggered_object_type'] = type(triggered_object).__name__
                        notification.save()
                        notifications.append(notification)
            
            # Update alert rule
            alert_rule.last_triggered = timezone.now()
            alert_rule.save()
            
            return notifications
            
        except Exception as e:
            logger.error(f"Error triggering alert rule {alert_rule.id}: {str(e)}")
            return notifications
    
    def test_alert_rule(self, alert_rule: AlertRule) -> bool:
        """Test an alert rule"""
        from django.apps import apps
        
        try:
            # Get model class
            app_label, model_name = alert_rule.model_name.split('.')
            model_class = apps.get_model(app_label, model_name)
            
            # Build filter
            filter_kwargs = self._build_alert_filter(alert_rule)
            
            # Check if any objects match
            matching_objects = model_class.objects.filter(**filter_kwargs)
            
            return matching_objects.exists()
            
        except Exception as e:
            logger.error(f"Error testing alert rule {alert_rule.id}: {str(e)}")
            return False
    
    def send_bulk_notification(
        self,
        bulk_notification_id: int
    ) -> Dict:
        """Send a bulk notification"""
        from ..models import BulkNotification
        
        try:
            bulk_notification = BulkNotification.objects.get(id=bulk_notification_id)
            
            # Update status
            bulk_notification.status = 'processing'
            bulk_notification.started_at = timezone.now()
            bulk_notification.save()
            
            # Get recipients (implement based on your customer model)
            # This is a placeholder - implement based on your customer model
            recipients = self._get_bulk_recipients(bulk_notification)
            bulk_notification.total_recipients = len(recipients)
            bulk_notification.save()
            
            # Send notifications
            success_count = 0
            failure_count = 0
            
            for recipient in recipients:
                try:
                    notification = Notification.objects.create(
                        notification_type=bulk_notification.notification_type,
                        subject=bulk_notification.subject,
                        message=bulk_notification.message,
                        recipient_email=recipient.get('email'),
                        recipient_phone=recipient.get('phone'),
                        metadata={
                            'bulk_notification_id': bulk_notification.id,
                            'recipient_data': recipient
                        }
                    )
                    
                    success = self.send_notification(notification)
                    if success:
                        success_count += 1
                    else:
                        failure_count += 1
                        
                except Exception as e:
                    logger.error(f"Error sending to recipient {recipient}: {str(e)}")
                    failure_count += 1
            
            # Update bulk notification
            bulk_notification.sent_count = success_count
            bulk_notification.failed_count = failure_count
            bulk_notification.status = 'completed'
            bulk_notification.completed_at = timezone.now()
            bulk_notification.save()
            
            return {
                'success': True,
                'sent': success_count,
                'failed': failure_count
            }
            
        except Exception as e:
            logger.error(f"Error sending bulk notification: {str(e)}")
            
            if bulk_notification:
                bulk_notification.status = 'failed'
                bulk_notification.save()
            
            return {
                'success': False,
                'error': str(e)
            }
    
    # Private methods
    def _send_sms(self, notification: Notification) -> bool:
        """Send SMS notification"""
        if not notification.recipient_phone:
            notification.mark_as_failed("No phone number provided")
            return False
        
        success, message_id, response = self.sms_service.send_sms(
            recipient=notification.recipient_phone,
            message=notification.message,
            metadata=notification.metadata
        )
        
        if success:
            notification.mark_as_sent()
            notification.metadata['provider_message_id'] = message_id
            notification.metadata['provider_response'] = response
            notification.save()
            return True
        else:
            notification.mark_as_failed(response.get('error', 'Unknown error'))
            return False
    
    def _send_email(self, notification: Notification) -> bool:
        """Send email notification"""
        if not notification.recipient_email:
            notification.mark_as_failed("No email address provided")
            return False
        
        success, message_id, response = self.email_service.send_email(
            recipient=notification.recipient_email,
            subject=notification.subject,
            message=notification.message,
            metadata=notification.metadata
        )
        
        if success:
            notification.mark_as_sent()
            notification.metadata['provider_message_id'] = message_id
            notification.metadata['provider_response'] = response
            notification.save()
            return True
        else:
            notification.mark_as_failed(response.get('error', 'Unknown error'))
            return False
    
    def _send_push(self, notification: Notification) -> bool:
        """Send push notification"""
        if not notification.recipient_device_token:
            notification.mark_as_failed("No device token provided")
            return False
        
        success, message_id, response = self.push_service.send_push(
            device_tokens=[notification.recipient_device_token],
            title=notification.subject or "Notification",
            body=notification.message,
            metadata=notification.metadata
        )
        
        if success:
            notification.mark_as_sent()
            notification.metadata['provider_message_id'] = message_id
            notification.metadata['provider_response'] = response
            notification.save()
            return True
        else:
            notification.mark_as_failed(response.get('error', 'Unknown error'))
            return False
    
    def _send_in_app(self, notification: Notification) -> bool:
        """Send in-app notification"""
        # In-app notifications are stored in database and retrieved via API
        notification.mark_as_sent()
        notification.save()
        return True
    
    def _get_alert_recipients(self, alert_rule: AlertRule) -> List:
        """Get recipients for an alert rule"""
        from django.contrib.auth import get_user_model
        User = get_user_model()
        
        recipients = []
        
        # Add specific users
        recipients.extend(list(alert_rule.specific_users.all()))
        
        # Add users by role
        if alert_rule.target_roles:
            for role in alert_rule.target_roles:
                # This assumes you have a role field on your User model
                # Adjust based on your actual user model
                role_users = User.objects.filter(groups__name=role)
                recipients.extend(list(role_users))
        
        # Remove duplicates
        unique_recipients = []
        seen_ids = set()
        for user in recipients:
            if user.id not in seen_ids:
                seen_ids.add(user.id)
                unique_recipients.append(user)
        
        return unique_recipients
    
    def _extract_variables(self, obj: Any) -> Dict:
        """Extract template variables from an object"""
        variables = {}
        
        if hasattr(obj, '__dict__'):
            # Add basic attributes
            for key, value in obj.__dict__.items():
                if not key.startswith('_'):
                    variables[key] = str(value)
        
        # Add common methods if they exist
        common_methods = ['get_absolute_url', 'get_full_name', 'email']
        for method in common_methods:
            if hasattr(obj, method):
                try:
                    result = getattr(obj, method)
                    if callable(result):
                        variables[method] = str(result())
                    else:
                        variables[method] = str(result)
                except:
                    pass
        
        return variables
    
    def _build_alert_filter(self, alert_rule: AlertRule) -> Dict:
        """Build Django filter from alert rule condition"""
        filter_kwargs = {}
        
        if alert_rule.condition_type == 'greater_than':
            filter_kwargs[f'{alert_rule.field_name}__gt'] = alert_rule.condition_value
        elif alert_rule.condition_type == 'less_than':
            filter_kwargs[f'{alert_rule.field_name}__lt'] = alert_rule.condition_value
        elif alert_rule.condition_type == 'equals':
            filter_kwargs[alert_rule.field_name] = alert_rule.condition_value
        elif alert_rule.condition_type == 'not_equals':
            filter_kwargs[f'{alert_rule.field_name}__ne'] = alert_rule.condition_value
        elif alert_rule.condition_type == 'contains':
            filter_kwargs[f'{alert_rule.field_name}__icontains'] = alert_rule.condition_value
        elif alert_rule.condition_type == 'starts_with':
            filter_kwargs[f'{alert_rule.field_name}__istartswith'] = alert_rule.condition_value
        elif alert_rule.condition_type == 'ends_with':
            filter_kwargs[f'{alert_rule.field_name}__iendswith'] = alert_rule.condition_value
        
        return filter_kwargs
    
    def _get_bulk_recipients(self, bulk_notification) -> List[Dict]:
        """Get recipients for bulk notification"""
        # This is a placeholder - implement based on your customer model
        # You should query your Customer model based on the target_segment
        
        recipients = []
        
        if bulk_notification.target_segment == 'custom_list':
            recipients = bulk_notification.custom_recipients or []
        
        # Add more segments as needed
        
        return recipients
    
    # Test methods
    def send_test_email(self, recipient: str, message: str) -> bool:
        """Send a test email"""
        success, _, _ = self.email_service.send_email(
            recipient=recipient,
            subject="Test Notification",
            message=message,
            metadata={'test': True}
        )
        return success
    
    def send_test_sms(self, recipient: str, message: str) -> bool:
        """Send a test SMS"""
        success, _, _ = self.sms_service.send_sms(
            recipient=recipient,
            message=message,
            metadata={'test': True}
        )
        return success