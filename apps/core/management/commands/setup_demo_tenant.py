"""
Management command to set up the demo.netily.co.ke tenant with sample data and demo login credentials.

Usage:
  python manage.py setup_demo_tenant

This creates (idempotently):
  - A demo company + tenant on schema 'demo'
  - Domain demo.netily.co.ke
  - An ISP admin user: admin@demo.netily.co.ke / DemoAdmin2026!
  - A customer user: +254700000000 / +254700000000
  - Sample internet plans
  - A sample service connection for the demo customer
"""
from datetime import date, timedelta
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone


class Command(BaseCommand):
    help = 'Set up demo.netily.co.ke tenant with sample data and demo logins'

    def handle(self, *args, **options):
        from apps.core.models import Tenant, Domain, Company, User

        self.stdout.write(self.style.NOTICE('=== Setting up Demo Tenant ==='))

        # ── 1. Company ──────────────────────────────────────────────────
        company, created = Company.objects.get_or_create(
            slug='demo',
            defaults={
                'name': 'Demo ISP',
                'company_type': 'isp',
                'email': 'admin@demo.netily.co.ke',
                'phone_number': '+254700000000',
                'address': 'Nairobi CBD',
                'city': 'Nairobi',
                'subscription_plan': 'enterprise',
                'subscription_expiry': date.today() + timedelta(days=365),
            },
        )
        self.stdout.write(f'  Company: {"CREATED" if created else "exists"} — {company.name}')

        # ── 2. Tenant ───────────────────────────────────────────────────
        try:
            tenant = Tenant.objects.get(schema_name='demo')
            self.stdout.write(f'  Tenant: exists — schema={tenant.schema_name}')
        except Tenant.DoesNotExist:
            tenant = Tenant(
                schema_name='demo',
                subdomain='demo',
                domain='demo.netily.co.ke',
                status='active',
                company=company,
                trial_start=date.today(),
                trial_days=365,
                subscription_expiry=date.today() + timedelta(days=365),
                max_users=100,
                max_customers=1000,
                next_billing_date=date.today() + timedelta(days=30),
            )
            tenant.save()
            with connection.cursor() as cursor:
                cursor.execute('CREATE SCHEMA "demo"')
            call_command('migrate_schemas_resilient', schema='demo')
            from apps.radius.services.tenant_radius_service import tenant_radius_service

            tenant_radius_service.configure_tenant_radius(
                schema_name='demo',
                tenant_name=company.name,
            )
            self.stdout.write(self.style.SUCCESS('  Tenant: CREATED — schema=demo'))

        # ── 3. Domain ───────────────────────────────────────────────────
        domain, created = Domain.objects.get_or_create(
            domain='demo.netily.co.ke',
            defaults={'tenant': tenant, 'is_primary': True},
        )
        self.stdout.write(f'  Domain: {"CREATED" if created else "exists"} — demo.netily.co.ke')

        # ── 4. Switch to demo schema ────────────────────────────────────
        connection.set_tenant(tenant)
        self.stdout.write('  Switched to demo schema')

        # ── 5. Admin user ───────────────────────────────────────────────
        # Note: core.0002_create_superadmin migration auto-creates a user
        # with phone +254700000001 in every new schema. We adopt it for demo.
        admin_email = 'admin@demo.netily.co.ke'
        admin_password = 'DemoAdmin2026!'
        admin_user = None

        # Try by email first
        try:
            admin_user = User.objects.get(email=admin_email)
            admin_user.set_password(admin_password)
            admin_user.save(update_fields=['password'])
            self.stdout.write(f'  Admin user: exists — password reset')
        except User.DoesNotExist:
            pass

        # Try to adopt the auto-created superadmin from migration
        if admin_user is None:
            try:
                admin_user = User.objects.get(phone_number='+254700000001')
                admin_user.email = admin_email
                admin_user.first_name = 'Demo'
                admin_user.last_name = 'Admin'
                admin_user.role = 'admin'
                admin_user.is_staff = True
                admin_user.set_password(admin_password)
                admin_user.save(update_fields=['email', 'first_name', 'last_name', 'role', 'is_staff', 'password'])
                self.stdout.write(self.style.SUCCESS(f'  Admin user: ADOPTED migration superadmin — {admin_email}'))
            except User.DoesNotExist:
                pass

        # Create fresh if still not found
        if admin_user is None:
            admin_user = User.objects.create_user(
                email=admin_email,
                phone_number='+254799000001',
                first_name='Demo',
                last_name='Admin',
                password=admin_password,
                role='admin',
                is_staff=True,
                is_superuser=False,
            )
            self.stdout.write(self.style.SUCCESS(f'  Admin user: CREATED — {admin_email}'))

        # ── 6. Customer user ────────────────────────────────────────────
        customer_phone = '+254700000000'
        customer_password = '+254700000000'
        try:
            customer_user = User.objects.get(phone_number=customer_phone)
            customer_user.set_password(customer_password)
            customer_user.save(update_fields=['password'])
            self.stdout.write(f'  Customer user: exists — password reset')
        except User.DoesNotExist:
            customer_user = User.objects.create_user(
                email='customer@demo.netily.co.ke',
                phone_number=customer_phone,
                first_name='John',
                last_name='Demo',
                password=customer_password,
                role='customer',
            )
            self.stdout.write(self.style.SUCCESS(f'  Customer user: CREATED — {customer_phone}'))

        # ── 7. Customer profile ─────────────────────────────────────────
        from apps.customers.models import Customer
        try:
            customer_profile = customer_user.customer_profile
            self.stdout.write(f'  Customer profile: exists — {customer_profile.customer_code}')
        except Customer.DoesNotExist:
            customer_profile = Customer.objects.create(
                user=customer_user,
                customer_code='DEMO-CUST-001',
                status='ACTIVE',
                customer_type='RESIDENTIAL',
                category='PREPAID',
                activation_date=date.today() - timedelta(days=15),
                outstanding_balance=Decimal('0.00'),
            )
            self.stdout.write(self.style.SUCCESS(f'  Customer profile: CREATED — {customer_profile.customer_code}'))

        # ── 8. Sample plans ─────────────────────────────────────────────
        from apps.billing.models import Plan
        demo_plans = [
            {
                'name': 'Starter 5Mbps',
                'code': 'DEMO-STARTER-5',
                'plan_type': 'PPPOE',
                'description': 'Affordable plan for light browsing and social media',
                'base_price': Decimal('500.00'),
                'download_speed': 5,
                'upload_speed': 5,
                'duration_days': 30,
                'is_active': True,
                'is_public': True,
                'is_popular': False,
                'features': ['Unlimited Data', 'Email Support'],
            },
            {
                'name': 'Home 10Mbps',
                'code': 'DEMO-HOME-10',
                'plan_type': 'PPPOE',
                'description': 'Perfect for small households with multiple devices',
                'base_price': Decimal('1000.00'),
                'download_speed': 10,
                'upload_speed': 10,
                'duration_days': 30,
                'is_active': True,
                'is_public': True,
                'is_popular': True,
                'features': ['Unlimited Data', '24/7 Support', 'Free Router Config'],
            },
            {
                'name': 'Family 25Mbps',
                'code': 'DEMO-FAMILY-25',
                'plan_type': 'PPPOE',
                'description': 'Great for families who stream and work from home',
                'base_price': Decimal('2000.00'),
                'download_speed': 25,
                'upload_speed': 25,
                'duration_days': 30,
                'is_active': True,
                'is_public': True,
                'is_popular': False,
                'features': ['Unlimited Data', '24/7 Support', 'Free Router Config', 'Priority Support'],
            },
            {
                'name': 'Business 50Mbps',
                'code': 'DEMO-BIZ-50',
                'plan_type': 'PPPOE',
                'description': 'High-speed plan for offices and heavy usage',
                'base_price': Decimal('5000.00'),
                'download_speed': 50,
                'upload_speed': 50,
                'duration_days': 30,
                'is_active': True,
                'is_public': True,
                'is_popular': False,
                'features': ['Unlimited Data', '24/7 Priority Support', 'Static IP', 'SLA Guarantee'],
            },
        ]

        popular_plan = None
        for plan_data in demo_plans:
            plan, created = Plan.objects.get_or_create(
                code=plan_data['code'],
                defaults={
                    'name': plan_data['name'],
                    'plan_type': plan_data['plan_type'],
                    'description': plan_data['description'],
                    'base_price': plan_data['base_price'],
                    'download_speed': plan_data['download_speed'],
                    'upload_speed': plan_data['upload_speed'],
                    'duration_days': plan_data['duration_days'],
                    'is_active': plan_data['is_active'],
                    'is_public': plan_data['is_public'],
                    'is_popular': plan_data['is_popular'],
                    'features': plan_data['features'],
                    'created_by': admin_user,
                },
            )
            if plan_data['is_popular']:
                popular_plan = plan
            status = 'CREATED' if created else 'exists'
            self.stdout.write(f'  Plan: {status} — {plan.name} (KSh {plan.base_price})')

        # ── 9. Service connection for demo customer ─────────────────────
        from apps.customers.models import ServiceConnection
        assigned_plan = popular_plan or Plan.objects.filter(is_active=True).first()
        if assigned_plan:
            svc, created = ServiceConnection.objects.get_or_create(
                customer=customer_profile,
                plan=assigned_plan,
                defaults={
                    'service_type': 'INTERNET',
                    'auth_connection_type': 'PPPOE',
                    'connection_type': 'FIBER',
                    'status': 'ACTIVE',
                    'activation_date': timezone.now() - timedelta(days=15),
                    'download_speed': assigned_plan.download_speed or 10,
                    'upload_speed': assigned_plan.upload_speed or 10,
                    'monthly_price': assigned_plan.base_price,
                },
            )
            self.stdout.write(f'  Service: {"CREATED" if created else "exists"} — {assigned_plan.name}')

        # ── 10. Assign plan to customer profile fallback ────────────────
        if assigned_plan and not customer_profile.plan_id:
            try:
                customer_profile.plan = assigned_plan
                customer_profile.save(update_fields=['plan'])
                self.stdout.write('  Assigned plan to customer profile')
            except Exception:
                pass

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=== Demo Tenant Setup Complete ==='))
        self.stdout.write(f'  URL:              https://demo.netily.co.ke')
        self.stdout.write(f'  Demo page:        https://demo.netily.co.ke/demo')
        self.stdout.write(f'  Admin login:      {admin_email} / {admin_password}')
        self.stdout.write(f'  Customer login:   {customer_phone} / {customer_password}')
