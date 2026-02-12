import os
import django

# 1. Setup Django Environment
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from django.db import connection
from apps.core.models import Tenant
from apps.network.models import Router
from apps.network.services.mikrotik_script_generator import MikrotikScriptGenerator

print('\n=== SCRIPT GENERATOR CHECK ===')

try:
    # 2. SWITCH SCHEMA
    # We look for 'test_isp' because that is the tenant we created earlier
    tenant = Tenant.objects.get(schema_name='test_isp')
    connection.set_tenant(tenant)
    print(f'✅ Context switched to tenant: {tenant.schema_name}')

    # 3. Get or Create a Test Router
    router, _ = Router.objects.get_or_create(
        name='SIM-ROUTER-1',
        defaults={
            'vpn_ip_address': '10.8.0.55',
            'hotspot_interface': 'bridge-hotspot',
            'local_ip_gateway': '10.5.50.1'
        }
    )
    print(f'✅ Using Router: {router.name} (ID: {router.id})')

    # 4. Generate Script
    generator = MikrotikScriptGenerator(router)
    script = generator.generate_full_script()
    
    # Simple check for key components
    if '/interface ovpn-client' in script:
        print('✅ PASS: Script generated successfully!')
        print(f'   - Size: {len(script)} characters')
        print('   - VPN Config: FOUND')
    else:
        print('❌ FAIL: Script generated but missing sections.')
        print('Preview:', script[:100])

except Exception as e:
    print(f'❌ FAIL: Error: {e}')