from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone
import datetime
from apps.core.models import Tenant, Domain, Company
from apps.network.models import Router
from apps.network.services.mikrotik_script_generator import MikrotikScriptGenerator

class Command(BaseCommand):
    help = 'Verifies Cloud Controller Backend Setup'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('=== 🛠️ STARTING SYSTEM VERIFICATION ==='))

        # ---------------------------------------------------------
        # 1. FIX TENANTS
        # ---------------------------------------------------------
        try:
            # Create Public
            c_pub, _ = Company.objects.get_or_create(
                name='Netily', 
                defaults={
                    'email': 'admin@netily.com', 'slug': 'netily', 'phone_number': '0700000000',
                    'address': 'Nairobi', 'city': 'Nairobi'
                }
            )
            if not Tenant.objects.filter(schema_name='public').exists():
                t = Tenant.objects.create(
                    schema_name='public', company=c_pub, subdomain='public', database_name='public',
                    trial_start=timezone.now(), subscription_expiry=timezone.now() + datetime.timedelta(days=365)
                )
                Domain.objects.create(domain='localhost', tenant=t, is_primary=True)
                self.stdout.write('✅ Public Tenant Created')
            
            # Create Test ISP
            c_isp, _ = Company.objects.get_or_create(
                name='Test ISP', 
                defaults={
                    'email': 'isp@test.com', 'slug': 'test-isp', 'phone_number': '0711111111',
                    'address': 'Nairobi', 'city': 'Nairobi'
                }
            )
            if not Tenant.objects.filter(schema_name='test_isp').exists():
                t_isp = Tenant.objects.create(
                    schema_name='test_isp', company=c_isp, subdomain='test-isp', database_name='test_isp',
                    trial_start=timezone.now(), subscription_expiry=timezone.now() + datetime.timedelta(days=365)
                )
                Domain.objects.create(domain='test.localhost', tenant=t_isp, is_primary=True)
                self.stdout.write(self.style.SUCCESS('✅ Tenant "test_isp" Created Successfully'))
            else:
                self.stdout.write('ℹ️ Tenant "test_isp" already exists')

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'❌ Tenant Setup Warning: {e}'))

        # ---------------------------------------------------------
        # 2. CHECK RADIUS (Raw SQL)
        # ---------------------------------------------------------
        self.stdout.write('\n--- Checking RADIUS DB ---')
        try:
            cursor = connection.cursor()
            cursor.execute("INSERT INTO radcheck (username, attribute, op, value) VALUES ('test_sim', 'Pass', ':=', '123') ON CONFLICT DO NOTHING")
            cursor.execute("INSERT INTO radreply (username, attribute, op, value) VALUES ('test_sim', 'Mikrotik-Rate-Limit', ':=', '5M/5M') ON CONFLICT DO NOTHING")
            self.stdout.write(self.style.SUCCESS('✅ RADIUS Tables Accessible & Writable'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'❌ RADIUS Check Failed: {e}'))

        # ---------------------------------------------------------
        # 3. CHECK SCRIPT GENERATOR
        # ---------------------------------------------------------
        self.stdout.write('\n--- Checking Script Factory ---')
        try:
            # Switch to Tenant
            tenant = Tenant.objects.get(schema_name='test_isp')
            connection.set_tenant(tenant)
            
            # Create Router (FIXED FIELDS)
            router, _ = Router.objects.get_or_create(
                name='SIM-ROUTER-1',
                defaults={
                    'vpn_ip_address': '10.8.0.55', 
                    'hotspot_interfaces': ['ether2', 'ether3'], # Fixed: List
                    'gateway_cidr': '10.5.50.1/24'              # Fixed: CIDR format
                }
            )
            
            # Generate Script
            gen = MikrotikScriptGenerator(router)
            script = gen.generate_full_script()
            
            if '/interface ovpn-client add' in script:
                self.stdout.write(self.style.SUCCESS('✅ Magic Script Generated Successfully'))
                self.stdout.write(f'   Size: {len(script)} bytes')
                if '10.8.0.55' in script:
                     self.stdout.write(f'   VPN Config: Valid')
            else:
                self.stdout.write(self.style.ERROR('❌ Script generated but missing VPN config'))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'❌ Script Gen Failed: {e}'))

        self.stdout.write(self.style.SUCCESS('\n=== 🏁 VERIFICATION COMPLETE ==='))