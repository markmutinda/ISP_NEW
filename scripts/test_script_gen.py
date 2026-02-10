"""Test Script Generator V3 - using mock Router object"""
import django, os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')
sys.path.insert(0, '/app')
django.setup()

from apps.network.services.mikrotik_script_generator import MikrotikScriptGenerator


class MockRouter:
    """Simulated Router object with all required attributes"""
    id = 999
    name = 'SIM-ROUTER-1'
    tenant_subdomain = 'testnet'
    ca_certificate = '-----BEGIN CERTIFICATE-----\nMOCK_CA_CERT_DATA\n-----END CERTIFICATE-----'
    client_certificate = '-----BEGIN CERTIFICATE-----\nMOCK_CLIENT_CERT_DATA\n-----END CERTIFICATE-----'
    client_key = '-----BEGIN PRIVATE KEY-----\nMOCK_CLIENT_KEY_DATA\n-----END PRIVATE KEY-----'
    vpn_ip_address = '10.8.0.55'
    openvpn_server = '10.8.0.1'
    openvpn_port = 1194
    openvpn_username = 'sim-router-1'
    api_username = 'netily_api'
    api_password = 'secure_api_pass'
    api_port = 8728
    gateway_cidr = '192.168.88.1/24'
    gateway_ip = '192.168.88.1'
    pool_range = '192.168.88.10-192.168.88.254'
    shared_secret = 'testing123'
    dns_name = 'hotspot.testnet.example.com'
    hotspot_interfaces = ['ether2', 'ether3']
    auth_key = 'sim-auth-key-12345'


router = MockRouter()
gen = MikrotikScriptGenerator(router)
script = gen.generate_full_script()

print("=" * 60)
print("SCRIPT GENERATOR V3 TEST")
print("=" * 60)
print("Script length: {} chars".format(len(script)))
print("Script lines: {} lines".format(len(script.splitlines())))
print()

sections = [
    'CLOUD CONTROLLER',
    'VPN',
    'CERTIFICATE',
    'RADIUS',
    'HOTSPOT',
    'WALLED GARDEN',
    'FIREWALL',
    'BRIDGE',
    'DHCP',
    'POOL',
    'REDIRECTOR',
]
print("--- Section Check ---")
for s in sections:
    found = s.upper() in script.upper()
    status = "PASS" if found else "FAIL"
    print("  [{}] Section: {}".format(status, s))

print()
print("--- Key Config Values ---")
checks = [
    ('Router name', 'SIM-ROUTER-1'),
    ('VPN IP', '10.8.0.55'),
    ('RADIUS server', '10.8.0.1'),
    ('RADIUS secret', 'testing123'),
    ('Hotspot DNS', 'hotspot.testnet.example.com'),
    ('Gateway IP', '192.168.88.1'),
    ('Pool range', '192.168.88.10-192.168.88.254'),
    ('API user', 'netily_api'),
    ('OpenVPN port', '1194'),
]
for label, value in checks:
    found = value in script
    status = "PASS" if found else "FAIL"
    print("  [{}] {}: {}".format(status, label, value))

print()
print("=== First 60 lines of generated script ===")
for i, line in enumerate(script.splitlines()[:60], 1):
    print("  {:3d} | {}".format(i, line))
print("  ... (truncated)")

# Also test VPN-only script
print()
print("=" * 60)
print("VPN-ONLY SCRIPT TEST")
print("=" * 60)
try:
    vpn_script = gen.generate_vpn_only_script()
    print("VPN-only script: {} chars, {} lines".format(len(vpn_script), len(vpn_script.splitlines())))
    print("[PASS] VPN-only script generated")
except Exception as e:
    print("[FAIL] VPN-only script: {}".format(e))
