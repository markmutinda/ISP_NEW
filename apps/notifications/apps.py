from django.apps import AppConfig
import logging

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
            
            # Create default notification templates if they don't exist
            self.create_default_templates()
            
            logger.info("Notifications app initialized successfully")
            
        except Exception as e:
            logger.warning(f"Notifications initialized with simulated services: {str(e)}")
    
    def create_default_templates(self):
        """Create default notification templates"""
        from .models import NotificationTemplate
        
        default_templates = [
            {
                'name': 'Welcome Email',
                'notification_type': 'email',
                'trigger_event': 'welcome',
                'subject': 'Welcome to {company_name}',
                'message_template': '''Dear {customer_name},

Welcome to {company_name}! We're excited to have you as our customer.

Your account has been successfully created. Here are your login details:
Username: {customer_email}
Password: [Temporary password will be sent separately]

To get started, please log in to your customer portal at {website_url}

If you have any questions, please contact our support team at {support_email} or {support_phone}.

Best regards,
The {company_name} Team''',
                'available_variables': 'company_name, customer_name, customer_email, website_url, support_email, support_phone',
                'priority': 3
            },
            {
                'name': 'Invoice Generated',
                'notification_type': 'email',
                'trigger_event': 'invoice_generated',
                'subject': 'Invoice #{invoice_number} from {company_name}',
                'message_template': '''Dear {customer_name},

Your invoice #{invoice_number} for {invoice_amount} KES has been generated.

Invoice Date: {invoice_date}
Due Date: {due_date}
Amount Due: {balance_due} KES

You can view and download your invoice here: {invoice_url}

Payment Methods:
1. M-Pesa Paybill: 123456
2. Bank Transfer
3. Cash Payment at our offices

Please ensure payment is made by the due date to avoid service interruption.

Best regards,
{company_name} Billing Department''',
                'available_variables': 'customer_name, invoice_number, invoice_amount, invoice_date, due_date, balance_due, invoice_url, company_name',
                'priority': 3
            },
            {
                'name': 'Payment Received',
                'notification_type': 'sms',
                'trigger_event': 'payment_received',
                'subject': '',
                'message_template': 'Dear {customer_name}, we have received your payment of {payment_amount} KES. Receipt #{receipt_number}. Thank you for paying on time. {company_name}',
                'available_variables': 'customer_name, payment_amount, receipt_number, company_name',
                'priority': 3
            },
            {
                'name': 'Service Activation',
                'notification_type': 'sms',
                'trigger_event': 'service_activation',
                'subject': '',
                'message_template': 'Dear {customer_name}, your {service_type} service has been activated. Your IP: {ip_address}. Bandwidth: {bandwidth}. Login details sent via email. Welcome to {company_name}!',
                'available_variables': 'customer_name, service_type, ip_address, bandwidth, company_name',
                'priority': 4
            },
            {
                'name': 'Low Balance Alert',
                'notification_type': 'sms',
                'trigger_event': 'low_balance',
                'subject': '',
                'message_template': 'Dear {customer_name}, your account balance is low ({balance_due} KES due). Please make payment to avoid service suspension. Pay via M-Pesa Paybill: 123456. {company_name}',
                'available_variables': 'customer_name, balance_due, company_name',
                'priority': 4
            },
            {
                'name': 'Ticket Created',
                'notification_type': 'email',
                'trigger_event': 'ticket_created',
                'subject': 'Support Ticket #{ticket_id} Created',
                'message_template': '''Dear {customer_name},

Thank you for contacting {company_name} Support.

Your support ticket has been created successfully.

Ticket Details:
Ticket ID: #{ticket_id}
Subject: {ticket_subject}
Priority: {ticket_priority}
Status: {ticket_status}

We will review your ticket and get back to you as soon as possible.

You can track the progress of your ticket by logging into your customer portal.

Best regards,
{company_name} Support Team''',
                'available_variables': 'customer_name, ticket_id, ticket_subject, ticket_priority, ticket_status, company_name',
                'priority': 3
            },
        ]
        
        for template_data in default_templates:
            if not NotificationTemplate.objects.filter(
                trigger_event=template_data['trigger_event']
            ).exists():
                NotificationTemplate.objects.create(**template_data)
                logger.info(f"Created default template: {template_data['name']}")
