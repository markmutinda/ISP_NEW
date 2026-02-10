"""Test Cloud Controller API Endpoints"""
import django, os, sys, json
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')
os.environ['DEBUG'] = 'False'  # Disable debug toolbar
sys.path.insert(0, '/app')
django.setup()

# Disable debug toolbar
from django.conf import settings
settings.DEBUG = False
if 'debug_toolbar' in settings.INSTALLED_APPS:
    settings.INSTALLED_APPS = [a for a in settings.INSTALLED_APPS if a != 'debug_toolbar']
if hasattr(settings, 'MIDDLEWARE'):
    settings.MIDDLEWARE = [m for m in settings.MIDDLEWARE if 'debug_toolbar' not in m]

from django.db import connection
from django.contrib.auth import get_user_model
from apps.core.models import Tenant

User = get_user_model()

print("=" * 60)
print("CLOUD CONTROLLER API ENDPOINT TESTS")
print("=" * 60)

# Switch to test_isp tenant schema
tenant = Tenant.objects.get(schema_name='test_isp')
connection.set_tenant(tenant)
print("[PASS] Switched to tenant schema: test_isp")

# Create test admin user in tenant schema
admin, created = User.objects.get_or_create(
    email='test@example.com',
    defaults={
        'first_name': 'Test',
        'last_name': 'Admin',
        'is_staff': True,
        'is_superuser': True,
    }
)
if created:
    admin.set_password('test123')
    admin.save()
    print("[PASS] Created test admin user")
else:
    print("[PASS] Test admin user exists")

from apps.network.models.router_models import Router

# Create a test router
router, created = Router.objects.get_or_create(
    name='SIM-ROUTER-1',
    defaults={
        'ip_address': '10.8.0.55',
        'api_username': 'netily_api',
        'api_password': 'secure123',
        'api_port': 8728,
        'config_type': 'hotspot',
        'shared_secret': 'testing123',
        'gateway_cidr': '192.168.88.1/24',
        'dns_name': 'hotspot.testnet.example.com',
        'openvpn_server': '10.8.0.1',
        'openvpn_port': 1194,
        'openvpn_username': 'sim-router-1',
        'vpn_ip_address': '10.8.0.55',
        'vpn_provisioned': True,
        'ca_certificate': '-----BEGIN CERTIFICATE-----\nMOCK_CA\n-----END CERTIFICATE-----',
        'client_certificate': '-----BEGIN CERTIFICATE-----\nMOCK_CLIENT\n-----END CERTIFICATE-----',
        'client_key': '-----BEGIN PRIVATE KEY-----\nMOCK_KEY\n-----END PRIVATE KEY-----',
        'tenant_subdomain': 'test',
    }
)
if created:
    print("[PASS] Created test router (pk={})".format(router.pk))
else:
    print("[PASS] Test router exists (pk={})".format(router.pk))

# Test script generation via the model method
print()
print("--- Script Generation via Router Model ---")
try:
    from apps.network.services.mikrotik_script_generator import MikrotikScriptGenerator
    gen = MikrotikScriptGenerator(router)
    script = gen.generate_full_script()
    print("[PASS] Full script: {} chars, {} lines".format(len(script), len(script.splitlines())))
except Exception as e:
    print("[FAIL] Script generation: {}".format(e))

# Test VPN-only script
try:
    vpn_script = gen.generate_vpn_only_script()
    print("[PASS] VPN-only script: {} chars".format(len(vpn_script)))
except Exception as e:
    print("[FAIL] VPN-only: {}".format(e))

# Test API endpoints using Django test client
print()
print("--- API Endpoint Tests ---")
from django.test import Client
# Use localhost (which is in ALLOWED_HOSTS) with X-Tenant header for tenant routing
client = Client(HTTP_HOST='localhost', HTTP_X_TENANT='test_isp')

# Login
client.force_login(admin)

# Test router list
try:
    resp = client.get('/api/v1/network/routers/')
    print("[{}] Router list: {} (count: {})".format(
        "PASS" if resp.status_code == 200 else "FAIL",
        resp.status_code,
        len(resp.json().get('results', resp.json())) if resp.status_code == 200 else 'N/A'
    ))
except Exception as e:
    print("[FAIL] Router list: {}".format(e))

# Test router detail
try:
    resp = client.get('/api/v1/network/routers/{}/'.format(router.pk))
    print("[{}] Router detail: {}".format(
        "PASS" if resp.status_code == 200 else "FAIL",
        resp.status_code
    ))
    if resp.status_code == 200:
        data = resp.json()
        vpn_fields = ['vpn_ip_address', 'vpn_provisioned', 'ca_certificate']
        for f in vpn_fields:
            has = f in data
            print("    [{}] Field '{}' in response".format("PASS" if has else "FAIL", f))
except Exception as e:
    print("[FAIL] Router detail: {}".format(e))

# Test config script endpoint
try:
    resp = client.get('/api/v1/network/routers/{}/config/'.format(router.pk))
    print("[{}] Config script endpoint: {} (len: {})".format(
        "PASS" if resp.status_code == 200 else "FAIL",
        resp.status_code,
        len(resp.content) if resp.status_code == 200 else 'N/A'
    ))
except Exception as e:
    print("[FAIL] Config script: {}".format(e))

# Test full-config endpoint
try:
    resp = client.get('/api/v1/network/routers/{}/full-config/'.format(router.pk))
    print("[{}] Full config: {}".format(
        "PASS" if resp.status_code == 200 else "FAIL",
        resp.status_code
    ))
except Exception as e:
    print("[FAIL] Full config: {}".format(e))

# Test VPN status endpoint
try:
    resp = client.get('/api/v1/network/routers/{}/vpn_status/'.format(router.pk))
    print("[{}] VPN status: {}".format(
        "PASS" if resp.status_code == 200 else "WARN",
        resp.status_code
    ))
except Exception as e:
    print("[FAIL] VPN status: {}".format(e))

# Test hotspot login page
try:
    resp = client.get('/api/v1/hotspot/login-page/{}/'.format(router.pk))
    print("[{}] Hotspot login page: {}".format(
        "PASS" if resp.status_code == 200 else "WARN",
        resp.status_code
    ))
except Exception as e:
    print("[FAIL] Hotspot login page: {}".format(e))

# Test hotspot plans
try:
    resp = client.get('/api/v1/hotspot/routers/{}/plans/'.format(router.pk))
    print("[{}] Hotspot plans: {} (count: {})".format(
        "PASS" if resp.status_code == 200 else "WARN",
        resp.status_code,
        len(resp.json()) if resp.status_code == 200 else 'N/A'
    ))
except Exception as e:
    print("[FAIL] Hotspot plans: {}".format(e))

# Test RADIUS dashboard
try:
    resp = client.get('/api/v1/radius/dashboard/')
    print("[{}] RADIUS dashboard: {}".format(
        "PASS" if resp.status_code == 200 else "WARN",
        resp.status_code
    ))
except Exception as e:
    print("[FAIL] RADIUS dashboard: {}".format(e))

# Test VPN dashboard
try:
    resp = client.get('/api/v1/vpn/dashboard/')
    print("[{}] VPN dashboard: {}".format(
        "PASS" if resp.status_code == 200 else "WARN",
        resp.status_code
    ))
except Exception as e:
    print("[FAIL] VPN dashboard: {}".format(e))

# Test router heartbeat
try:
    resp = client.post('/api/v1/network/routers/heartbeat/', 
        data=json.dumps({'router_name': 'SIM-ROUTER-1', 'vpn_ip': '10.8.0.55'}),
        content_type='application/json'
    )
    print("[{}] Router heartbeat: {}".format(
        "PASS" if resp.status_code in [200, 201] else "WARN",
        resp.status_code
    ))
except Exception as e:
    print("[FAIL] Router heartbeat: {}".format(e))

# Test RADIUS active sessions
try:
    resp = client.get('/api/v1/radius/sessions/active/')
    print("[{}] RADIUS active sessions: {}".format(
        "PASS" if resp.status_code == 200 else "WARN",
        resp.status_code
    ))
except Exception as e:
    print("[FAIL] RADIUS sessions: {}".format(e))

print()
print("=" * 60)
print("TEST COMPLETE — Router pk={}, name={}".format(router.pk, router.name))
print("=" * 60)
