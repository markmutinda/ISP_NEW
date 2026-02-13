from django.core.management.base import BaseCommand
from django.db import connection
from apps.core.models import Tenant
from apps.network.models import Router
import socket

class Command(BaseCommand):
    help = 'Generates the MikroTik Magic Link'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('\n=== 🚀 GENERATING MAGIC LINK ==='))

        try:
            # 1. Switch to Tenant
            try:
                tenant = Tenant.objects.get(schema_name='test_isp')
                connection.set_tenant(tenant)
            except Tenant.DoesNotExist:
                self.stdout.write(self.style.ERROR('❌ Error: Tenant "test_isp" not found. Run verify_setup first.'))
                return

            # 2. Get Router Key
            try:
                router = Router.objects.get(name='SIM-ROUTER-1')
                key = router.auth_key
            except Router.DoesNotExist:
                self.stdout.write(self.style.ERROR('❌ Error: Router "SIM-ROUTER-1" not found.'))
                return

            # 3. Detect IP (or fallback to the one you used)
            # We default to the one you used for VPN setup: 192.168.50.2
            ip = '192.168.50.2' 
            
            # Construct the Command
            url = f"http://{ip}:8000/api/v1/network/routers/config/?auth_key={key}"
            
            self.stdout.write('\n' + '='*60)
            self.stdout.write('📋 COPY AND PASTE THIS INTO YOUR MIKROTIK TERMINAL:')
            self.stdout.write('='*60 + '\n')
            
            self.stdout.write(self.style.WARNING(f'/tool fetch url="{url}" dst-path=netily_setup.rsc mode=http;'))
            self.stdout.write(self.style.WARNING(':delay 5s;'))
            self.stdout.write(self.style.WARNING('/import netily_setup.rsc;'))
            
            self.stdout.write('\n' + '='*60)
            self.stdout.write(f'ℹ️  Ensure your Django server is running on {ip}:8000')
            self.stdout.write('='*60 + '\n')

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'❌ Error: {e}'))