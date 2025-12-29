"""
Command to initialize system with default settings
"""
from django.core.management.base import BaseCommand
from apps.core.models import SystemSettings, Company, User
from django.utils import timezone


class Command(BaseCommand):
    help = 'Initialize system with default settings and data'

    def handle(self, *args, **options):
        self.stdout.write(self.style.HTTP_INFO('Initializing system...'))
        
        # Create default system settings
        default_settings = [
            {
                'key': 'company_name',
                'name': 'Company Name',
                'value': 'ISP Management System',
                'setting_type': 'general',
                'data_type': 'string',
                'is_public': True,
                'description': 'Name of the ISP company',
            },
            {
                'key': 'currency',
                'name': 'Currency',
                'value': 'KES',
                'setting_type': 'general',
                'data_type': 'string',
                'is_public': True,
                'description': 'Default currency',
            },
            {
                'key': 'timezone',
                'name': 'Timezone',
                'value': 'Africa/Nairobi',
                'setting_type': 'general',
                'data_type': 'string',
                'is_public': True,
                'description': 'System timezone',
            },
            {
                'key': 'vat_rate',
                'name': 'VAT Rate',
                'value': '16',
                'setting_type': 'billing',
                'data_type': 'float',
                'is_public': True,
                'description': 'Value Added Tax rate (%)',
            },
            {
                'key': 'invoice_prefix',
                'name': 'Invoice Prefix',
                'value': 'INV',
                'setting_type': 'billing',
                'data_type': 'string',
                'is_public': True,
                'description': 'Prefix for invoice numbers',
            },
            {
                'key': 'smtp_host',
                'name': 'SMTP Host',
                'value': 'smtp.gmail.com',
                'setting_type': 'email',
                'data_type': 'string',
                'is_public': False,
                'description': 'SMTP server hostname',
            },
            {
                'key': 'smtp_port',
                'name': 'SMTP Port',
                'value': '587',
                'setting_type': 'email',
                'data_type': 'integer',
                'is_public': False,
                'description': 'SMTP server port',
            },
            {
                'key': 'sms_enabled',
                'name': 'SMS Enabled',
                'value': 'false',
                'setting_type': 'sms',
                'data_type': 'boolean',
                'is_public': False,
                'description': 'Enable/disable SMS notifications',
            },
            {
                'key': 'max_login_attempts',
                'name': 'Max Login Attempts',
                'value': '5',
                'setting_type': 'security',
                'data_type': 'integer',
                'is_public': False,
                'description': 'Maximum failed login attempts before lockout',
            },
            {
                'key': 'session_timeout',
                'name': 'Session Timeout',
                'value': '30',
                'setting_type': 'security',
                'data_type': 'integer',
                'is_public': False,
                'description': 'Session timeout in minutes',
            },
        ]
        
        created_count = 0
        for setting_data in default_settings:
            key = setting_data['key']
            
            if not SystemSettings.objects.filter(key=key).exists():
                SystemSettings.objects.create(**setting_data)
                created_count += 1
                self.stdout.write(f"Created setting: {key}")
        
        self.stdout.write(self.style.SUCCESS(
            f'Created {created_count} system settings'
        ))
        
        # Create default company if none exists
        if not Company.objects.exists():
            default_company = {
                'name': 'Default ISP Company',
                'company_type': 'isp',
                'email': 'info@ispcompany.com',
                'phone_number': '+254712345678',
                'address': '123 Main Street\nNairobi, Kenya',
                'city': 'Nairobi',
                'county': 'Nairobi',
                'postal_code': '00100',
                'registration_number': 'C12345678',
                'tax_pin': 'A001234567M',
                'is_active': True,
            }
            
            # Get first admin user for created_by
            admin_user = User.objects.filter(role='admin').first()
            if admin_user:
                default_company['created_by'] = admin_user
            
            Company.objects.create(**default_company)
            self.stdout.write(self.style.SUCCESS('Created default company'))
        
        self.stdout.write(self.style.SUCCESS('System initialization completed!'))