import os
import django

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from django.db import connection
from apps.core.models import Tenant
from apps.network.models import Router

try:
    # 1. Switch to Tenant
    tenant = Tenant.objects.get(schema_name='test_isp')
    connection.set_tenant(tenant)

    # 2. Get Router Key
    router = Router.objects.get(name='SIM-ROUTER-1')
    key = router.auth_key

    # 3. Your Laptop IP 
    # (Based on your certificate generation logs, this is your IP)
    ip = '192.168.50.2' 

    print('\n' + '='*60)
    print('🚀 YOUR MIKROTIK ONE-LINER')
    print('='*60)
    # Note: We use simple concatenation to avoid f-string escaping issues in some environments
    url = "http://" + ip + ":8000/api/v1/network/routers/config/?auth_key=" + str(key)
    
    print('/tool fetch url="' + url + '" dst-path=netily_setup.rsc mode=http;')
    print(':delay 5s;')
    print('/import netily_setup.rsc;')
    print('='*60 + '\n')

except Exception as e:
    print(f"Error: {e}")
    