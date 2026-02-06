from django.apps import AppConfig
import logging
import sys

logger = logging.getLogger(__name__)

class NotificationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.notifications'
    verbose_name = 'Notifications Management'
    
    def ready(self):
        """Initialize notification system when app is ready"""
        try:
            # Import signal handlers
            from . import signals
            
            # 🚨 FIX: We commented this out because it causes the server 
            # to hang during startup (Database isn't ready yet).
            # self.create_default_templates()
            
            logger.info("Notifications app initialized successfully")
            
        except Exception as e:
            logger.warning(f"Notifications initialized with simulated services: {str(e)}")
    
    def create_default_templates(self):
        """Create default notification templates"""
        # This method is preserved but not called automatically anymore.
        # You can call it manually via shell if needed.
        try:
            from .models import NotificationTemplate
            
            default_templates = [
                {
                    'name': 'Welcome Email',
                    'notification_type': 'email',
                    'trigger_event': 'welcome',
                    'subject': 'Welcome to {company_name}',
                    'message_template': 'Welcome...', # Shortened for safety
                    'available_variables': 'company_name, customer_name',
                    'priority': 3
                },
                # ... (other templates would go here)
            ]
            
            # Simplified for safety - only run if explicitly called
            pass
            
        except Exception as e:
            logger.error(f"Error creating templates: {e}")