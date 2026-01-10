from django.core.management.base import BaseCommand
from django.utils import timezone
from apps.billing.models.billing_models import Plan
from apps.core.models import Company, User
import json


class Command(BaseCommand):
    help = 'Create sample plans for testing'

    def handle(self, *args, **kwargs):
        # Get or create a company
        company = Company.objects.first()
        if not company:
            company = Company.objects.create(
                name='Default ISP Company',
                code='DEFAULT',
                email='info@defaultisp.com',
                phone='+254700000000'
            )
        
        # Get admin user
        admin_user = User.objects.filter(is_superuser=True).first()
        if not admin_user:
            admin_user = User.objects.create_superuser(
                email='admin@defaultisp.com',
                phone='+254711111111',
                first_name='Admin',
                last_name='User',
                password='admin123'
            )
        
        sample_plans = [
            {
                'name': 'Home Basic 10Mbps',
                'plan_type': 'PPPOE',
                'description': 'Perfect for small households with basic internet needs',
                'base_price': 2500.00,
                'setup_fee': 500.00,
                'download_speed': 10,
                'upload_speed': 5,
                'data_limit': None,
                'duration_days': 30,
                'is_active': True,
                'is_public': True,
                'is_popular': False,
                'features': ['Unlimited Data', '24/7 Support', 'Free Installation']
            },
            {
                'name': 'Home Plus 25Mbps',
                'plan_type': 'PPPOE',
                'description': 'Great for families with multiple devices',
                'base_price': 3500.00,
                'setup_fee': 500.00,
                'download_speed': 25,
                'upload_speed': 10,
                'data_limit': None,
                'duration_days': 30,
                'is_active': True,
                'is_public': True,
                'is_popular': True,
                'features': ['Unlimited Data', '24/7 Support', 'Free Installation', 'Priority Support']
            },
            {
                'name': 'Business Standard 50Mbps',
                'plan_type': 'STATIC',
                'description': 'Ideal for small businesses with static IP requirement',
                'base_price': 8000.00,
                'setup_fee': 1000.00,
                'download_speed': 50,
                'upload_speed': 25,
                'data_limit': None,
                'duration_days': 30,
                'is_active': True,
                'is_public': True,
                'is_popular': True,
                'features': ['Static IP', 'Unlimited Data', '24/7 Support', 'Business Priority', 'SLA 99.5%']
            },
            {
                'name': 'Hotspot Daily 5Mbps',
                'plan_type': 'HOTSPOT',
                'description': 'Daily internet access for public hotspots',
                'base_price': 100.00,
                'setup_fee': 0.00,
                'download_speed': 5,
                'upload_speed': 2,
                'data_limit': 2,
                'duration_days': 1,
                'validity_hours': 24,
                'is_active': True,
                'is_public': True,
                'is_popular': False,
                'features': ['Pay as you go', 'Instant Activation', 'No Contract']
            },
            {
                'name': 'Gaming Pro 100Mbps',
                'plan_type': 'INTERNET',
                'description': 'Ultra-fast internet for gaming and streaming',
                'base_price': 12000.00,
                'setup_fee': 1500.00,
                'download_speed': 100,
                'upload_speed': 50,
                'data_limit': None,
                'duration_days': 30,
                'fup_limit': 1000,
                'fup_speed': 20,
                'is_active': True,
                'is_public': True,
                'is_popular': True,
                'features': ['Low Latency', 'Unlimited Data', 'Gaming Priority', '24/7 Support', 'Free Router']
            },
            {
                'name': 'Add-on Static IP',
                'plan_type': 'ADDON',
                'description': 'Additional static IP address',
                'base_price': 500.00,
                'setup_fee': 0.00,
                'download_speed': None,
                'upload_speed': None,
                'data_limit': None,
                'duration_days': 30,
                'is_active': True,
                'is_public': True,
                'is_popular': False,
                'features': ['Dedicated IP', 'Easy Setup', 'No Speed Limits']
            },
            {
                'name': 'Data Top-up 5GB',
                'plan_type': 'TOPUP',
                'description': 'Additional data for limited plans',
                'base_price': 200.00,
                'setup_fee': 0.00,
                'download_speed': None,
                'upload_speed': None,
                'data_limit': 5,
                'duration_days': 7,
                'is_active': True,
                'is_public': True,
                'is_popular': False,
                'features': ['Instant Activation', 'Rollover Option', 'No Contract']
            }
        ]
        
        created_count = 0
        for plan_data in sample_plans:
            # Check if plan already exists
            if not Plan.objects.filter(name=plan_data['name'], company=company).exists():
                plan = Plan.objects.create(
                    company=company,
                    created_by=admin_user,
                    updated_by=admin_user,
                    **plan_data
                )
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f'Created plan: {plan.name}')
                )
        
        self.stdout.write(
            self.style.SUCCESS(f'Successfully created {created_count} sample plans')
        )