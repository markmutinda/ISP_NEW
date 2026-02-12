import os
import django
from django.test import RequestFactory

# Setup Django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from apps.core.models import Tenant
from apps.network.models import Router
from apps.network.views.router_views import RouterViewSet
from django.db import connection

print('\n=== 🕵️‍♂️ API DEBUGGER ===')

try:
    # 1. Simulate the Tenant Context
    tenant = Tenant.objects.get(schema_name='test_isp')
    connection.set_tenant(tenant)
    print(f'✅ Context: {tenant.schema_name}')

    # 2. Get the Key
    router = Router.objects.get(name='SIM-ROUTER-1')
    key = router.auth_key
    print(f'🔑 Testing Key: {key}')

    # 3. Simulate the Request (Bypassing Network/Host headers)
    factory = RequestFactory()
    # Construct request for /config/ action with auth_key
    request = factory.get(f'/api/v1/network/routers/config/?auth_key={key}')
    request.tenant = tenant # Inject tenant manually like middleware does

    # 4. Instantiate View and Run
    view = RouterViewSet.as_view({'get': 'config'})
    response = view(request)

    print('\n=== RESULTS ===')
    print(f'Status Code: {response.status_code}')
    
    if response.status_code == 200:
        print('✅ SUCCESS! The View logic is perfect.')
        print('👉 The issue is DEFINITELY network/settings (ALLOWED_HOSTS).')
    elif response.status_code == 401:
        print('❌ 401 UNAUTHORIZED')
        print('👉 The issue is in the CODE. RouterViewSet is rejecting the key.')
        print('👉 Check: Does RouterViewSet allow "AllowAny" permission?')
    else:
        print(f'❌ Failed with {response.status_code}')
        print(response.data)

except Exception as e:
    print(f'❌ CRITICAL ERROR: {e}')