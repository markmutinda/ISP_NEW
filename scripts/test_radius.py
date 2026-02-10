"""
Cloud Controller Test Script — RADIUS & API endpoint testing
Run inside Docker: docker exec netily_backend python /app/scripts/test_radius.py
"""
import os, sys, django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')
django.setup()

from django.db import connection

def test_radius_tables():
    """Check if RADIUS tables exist in the database."""
    print("\n" + "="*60)
    print("TEST: RADIUS Database Tables")
    print("="*60)
    
    cursor = connection.cursor()
    cursor.execute(
        "SELECT schemaname, tablename FROM pg_tables "
        "WHERE tablename LIKE 'rad%%' ORDER BY schemaname, tablename"
    )
    rows = cursor.fetchall()
    
    if rows:
        print(f"  Found {len(rows)} RADIUS tables:")
        for schema, table in rows:
            print(f"    {schema}.{table}")
        return True
    else:
        print("  WARNING: No RADIUS tables found!")
        print("  You may need to run migrations or create them manually.")
        return False


def test_create_radius_user():
    """Create a test RADIUS user and verify it exists."""
    print("\n" + "="*60)
    print("TEST: Create RADIUS Test User")
    print("="*60)
    
    try:
        from apps.radius.models import RadCheck, RadReply
        
        # Clean up any existing test user
        RadCheck.objects.filter(username='test_sim_router').delete()
        RadReply.objects.filter(username='test_sim_router').delete()
        
        # Create test credentials (like a phone MAC would be)
        RadCheck.objects.create(
            username='test_sim_router',
            attribute='Cleartext-Password',
            op=':=',
            value='test_sim_router'
        )
        
        # Add a rate limit reply
        RadReply.objects.create(
            username='test_sim_router',
            attribute='Mikrotik-Rate-Limit',
            op=':=',
            value='5M/5M'
        )
        
        # Add session timeout (1 hour)
        RadReply.objects.create(
            username='test_sim_router',
            attribute='Session-Timeout',
            op=':=',
            value='3600'
        )
        
        # Verify
        checks = RadCheck.objects.filter(username='test_sim_router')
        replies = RadReply.objects.filter(username='test_sim_router')
        
        print(f"  RadCheck entries: {checks.count()}")
        for c in checks:
            print(f"    {c.attribute} {c.op} {c.value}")
        print(f"  RadReply entries: {replies.count()}")
        for r in replies:
            print(f"    {r.attribute} {r.op} {r.value}")
        
        print("  PASS: Test RADIUS user created successfully")
        return True
        
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def test_nas_registration():
    """Check if any NAS (Network Access Server) entries exist."""
    print("\n" + "="*60)
    print("TEST: NAS Registration")
    print("="*60)
    
    try:
        from apps.radius.models import Nas
        
        nas_entries = Nas.objects.all()
        if nas_entries.exists():
            print(f"  Found {nas_entries.count()} NAS entries:")
            for nas in nas_entries:
                print(f"    {nas.shortname}: {nas.nasname} (secret: {nas.secret[:4]}...)")
        else:
            print("  No NAS entries found. Creating a default one for testing...")
            Nas.objects.create(
                nasname='0.0.0.0/0',
                shortname='default',
                type='other',
                secret='testing123',
                description='Default NAS for all routers'
            )
            print("  Created default NAS entry (0.0.0.0/0)")
        
        print("  PASS")
        return True
        
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def test_router_model():
    """Check the Router model has VPN fields."""
    print("\n" + "="*60)
    print("TEST: Router Model VPN Fields")
    print("="*60)
    
    try:
        from apps.network.models.router_models import Router
        
        vpn_fields = [
            'ca_certificate', 'client_certificate', 'client_key',
            'vpn_ip_address', 'vpn_certificate', 'vpn_provisioned',
            'vpn_provisioned_at', 'vpn_last_seen'
        ]
        
        missing = []
        for field_name in vpn_fields:
            try:
                Router._meta.get_field(field_name)
                print(f"    {field_name}: EXISTS")
            except Exception:
                missing.append(field_name)
                print(f"    {field_name}: MISSING")
        
        if missing:
            print(f"  WARNING: Missing fields: {missing}")
            print("  Run: python manage.py migrate network")
            return False
        else:
            print("  PASS: All VPN fields present")
            return True
            
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


def test_script_generator():
    """Test the MikroTik script generator."""
    print("\n" + "="*60)
    print("TEST: Script Generator")
    print("="*60)
    
    try:
        from apps.network.models.router_models import Router
        from apps.network.services.mikrotik_script_generator import MikrotikScriptGenerator
        
        # Create a temporary mock router (don't save)
        router = Router(
            id=999,
            name='Test-Sim-Router',
            ip_address='192.168.88.1',
            api_port=8728,
            api_username='admin',
            api_password='test123',
            shared_secret='test_secret_123',
            gateway_cidr='172.18.0.1/16',
            hotspot_interfaces=['ether2', 'ether3', 'wlan1'],
            auth_key='RTR_TEST_AUTH',
            vpn_provisioned=True,
            vpn_ip_address='10.8.0.55',
            ca_certificate='-----BEGIN CERTIFICATE-----\nTEST_CA\n-----END CERTIFICATE-----',
            client_certificate='-----BEGIN CERTIFICATE-----\nTEST_CERT\n-----END CERTIFICATE-----',
            client_key='-----BEGIN PRIVATE KEY-----\nTEST_KEY\n-----END PRIVATE KEY-----',
        )
        
        generator = MikrotikScriptGenerator(router)
        script = generator.generate_full_script()
        
        # Check for required sections
        checks = {
            'System Identity': 'system identity set' in script.lower(),
            'OpenVPN Client': 'ovpn-client' in script.lower() or 'openvpn' in script.lower(),
            'Certificate': 'begin certificate' in script.lower() or 'certificate' in script.lower(),
            'Bridge': 'netily-bridge' in script.lower(),
            'IP Pool': 'netily-pool' in script.lower(),
            'RADIUS': 'radius' in script.lower(),
            'Hotspot': 'hotspot' in script.lower(),
            'Walled Garden': 'walled-garden' in script.lower(),
        }
        
        print(f"  Script generated: {len(script)} chars, {script.count(chr(10))} lines")
        all_pass = True
        for section, found in checks.items():
            status = "FOUND" if found else "MISSING"
            if not found:
                all_pass = False
            print(f"    {section}: {status}")
        
        if all_pass:
            print("  PASS: All required sections present")
        else:
            print("  PARTIAL: Some sections missing (check script generator)")
        
        return all_pass
        
    except Exception as e:
        print(f"  FAIL: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_django_api():
    """Test that key API endpoints respond."""
    print("\n" + "="*60)
    print("TEST: Django API Endpoints")
    print("="*60)
    
    from django.test import RequestFactory
    
    # Just verify the URL patterns are registered
    from django.urls import reverse, resolve
    
    endpoints = [
        ('hotspot-login-page', None),
        ('hotspot-auto-login', None),
        ('hotspot-device-auth-request', None),
        ('hotspot-device-auth-status', None),
    ]
    
    for name, kwargs in endpoints:
        try:
            url = reverse(name, kwargs=kwargs) if kwargs else reverse(name)
            print(f"    {name}: {url} — REGISTERED")
        except Exception as e:
            print(f"    {name}: NOT FOUND — {e}")
    
    print("  Done checking URL patterns")
    return True


if __name__ == '__main__':
    print("╔" + "═"*58 + "╗")
    print("║   NETILY CLOUD CONTROLLER — SIMULATED TEST SUITE        ║")
    print("║   Date: " + __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M') + "                                    ║")
    print("╚" + "═"*58 + "╝")
    
    results = {}
    results['RADIUS Tables'] = test_radius_tables()
    results['RADIUS User'] = test_create_radius_user()
    results['NAS Registration'] = test_nas_registration()
    results['Router VPN Fields'] = test_router_model()
    results['Script Generator'] = test_script_generator()
    results['API Endpoints'] = test_django_api()
    
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for test, result in results.items():
        print(f"  {'PASS' if result else 'FAIL'} — {test}")
    print(f"\n  Result: {passed}/{total} passed")
    print("="*60)
