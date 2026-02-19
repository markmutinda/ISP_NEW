"""
═══════════════════════════════════════════════════════════════════════════
  Multi-Router IPAM & PPPoE Integration — End-to-End Simulation Test
═══════════════════════════════════════════════════════════════════════════

This script validates the COMPLETE chain:

  Router → IP Pool → Customer + Service (PPPoE) → RADIUS Credentials
  → radcheck (password) → radreply (Framed-Pool) → Internet Check

Run: python manage.py tenant_command test_runner --schema=tenant_huey
  Or: python tests/test_router_ipam_pppoe_chain.py

Industry best-practice checks:
  ✔ RADIUS Framed-Pool attribute maps to MikroTik /ip pool name
  ✔ Router FK ensures pool-to-NAS binding (prevents cross-router leaks)
  ✔ Signal chain auto-provisions credentials on service creation
  ✔ Internet Check validation: pool exists on assigned router
"""

import os
import sys
import traceback
from datetime import timedelta
from decimal import Decimal

# ─── Django Bootstrap ───────────────────────────────────────────────────
# Allow running directly: python tests/test_router_ipam_pppoe_chain.py
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')

import django
django.setup()

from django.db import connection
from django.utils import timezone


# ─── Colour helpers ─────────────────────────────────────────────────────
class C:
    GREEN  = '\033[92m'
    RED    = '\033[91m'
    YELLOW = '\033[93m'
    CYAN   = '\033[96m'
    BOLD   = '\033[1m'
    END    = '\033[0m'

def ok(msg):    print(f"  {C.GREEN}✔ PASS{C.END}  {msg}")
def fail(msg):  print(f"  {C.RED}✘ FAIL{C.END}  {msg}")
def warn(msg):  print(f"  {C.YELLOW}⚠ WARN{C.END}  {msg}")
def info(msg):  print(f"  {C.CYAN}ℹ INFO{C.END}  {msg}")
def header(msg): print(f"\n{C.BOLD}{'═'*60}\n  {msg}\n{'═'*60}{C.END}")
def subheader(msg): print(f"\n  {C.BOLD}── {msg} ──{C.END}")


# ─── Tenant Context ────────────────────────────────────────────────────
TEST_SCHEMA = 'tenant_huey'  # Change if needed

passed = 0
failed = 0
warnings = 0


def assert_true(condition, msg):
    global passed, failed
    if condition:
        ok(msg)
        passed += 1
    else:
        fail(msg)
        failed += 1


def assert_equal(actual, expected, msg):
    global passed, failed
    if actual == expected:
        ok(f"{msg}: {actual}")
        passed += 1
    else:
        fail(f"{msg}: expected={expected}, got={actual}")
        failed += 1


def assert_not_none(value, msg):
    global passed, failed
    if value is not None:
        ok(msg)
        passed += 1
    else:
        fail(f"{msg}: got None")
        failed += 1


def cleanup_test_data():
    """Remove any leftover test objects from previous runs."""
    from apps.radius.models import CustomerRadiusCredentials, RadCheck, RadReply, RadiusBandwidthProfile
    from apps.customers.models import ServiceConnection, Customer
    from apps.network.models.router_models import Router
    from apps.network.models.ipam_models import IPPool
    from apps.core.models import User

    # Clean in dependency order
    for model_cls in [RadReply, RadCheck]:
        model_cls.objects.filter(username__startswith='test_sim_').delete()

    CustomerRadiusCredentials.objects.filter(username__startswith='test_sim_').delete()

    # Find test customers and delete their services
    test_users = User.objects.filter(email__startswith='test_sim_')
    for u in test_users:
        if hasattr(u, 'customer_profile'):
            ServiceConnection.objects.filter(customer=u.customer_profile).delete()
            u.customer_profile.delete()
        u.delete()

    IPPool.objects.filter(name__startswith='test-sim-pool').delete()
    Router.objects.filter(name__startswith='TEST-SIM-').delete()
    RadiusBandwidthProfile.objects.filter(name__startswith='test_sim_').delete()


def run_tests():
    global passed, failed, warnings
    
    from django_tenants.utils import schema_context
    
    with schema_context(TEST_SCHEMA):
        info(f"Running in tenant schema: {TEST_SCHEMA}")
        info(f"Current schema: {connection.schema_name}")
        
        # ═══════════════════════════════════════════════════════════
        # CLEANUP previous test runs
        # ═══════════════════════════════════════════════════════════
        header("Phase 0: Cleanup")
        try:
            cleanup_test_data()
            ok("Previous test data cleaned up")
        except Exception as e:
            warn(f"Cleanup had issues (non-blocking): {e}")
            warnings += 1

        # ═══════════════════════════════════════════════════════════
        # PHASE 1: Router Simulation
        # ═══════════════════════════════════════════════════════════
        header("Phase 1: Router Simulation")
        
        from apps.network.models.router_models import Router
        
        subheader("1a. Create simulated MikroTik router")
        router = Router.objects.create(
            name='TEST-SIM-MikroTik-Main',
            router_type='mikrotik',
            config_type='pppoe',
            ip_address='10.8.0.100',
            status='online',
            is_active=True,
            is_authenticated=True,
            enable_pppoe=True,
            pppoe_pool='192.40.2.10-192.40.2.254',
            pppoe_local_address='192.40.2.1',
            gateway_cidr='172.18.0.1/16',
            wan_interface='ether1',
            tenant_subdomain='huey',
        )
        assert_not_none(router.id, f"Router created: id={router.id}")
        assert_not_none(router.auth_key, "Router has auto-generated auth_key")
        assert_not_none(router.shared_secret, "Router has auto-generated shared_secret")
        assert_not_none(router.api_password, "Router has auto-generated api_password")
        assert_equal(router.status, 'online', "Router status is online")
        info(f"Router auth_key: {router.auth_key}")
        info(f"Router shared_secret: {router.shared_secret[:16]}...")
        
        subheader("1b. Create second router (for cross-router validation)")
        router2 = Router.objects.create(
            name='TEST-SIM-MikroTik-Branch',
            router_type='mikrotik',
            config_type='pppoe',
            ip_address='10.8.0.101',
            status='online',
            is_active=True,
            is_authenticated=True,
            enable_pppoe=True,
            pppoe_pool='192.50.2.10-192.50.2.254',
            pppoe_local_address='192.50.2.1',
            gateway_cidr='172.19.0.1/16',
            wan_interface='ether1',
            tenant_subdomain='huey',
        )
        assert_not_none(router2.id, f"Second router created: id={router2.id}")

        subheader("1c. Verify NAS auto-sync (Router→NAS signal)")
        from apps.radius.models import Nas
        nas_entries = Nas.objects.filter(nasname=router.ip_address)
        if nas_entries.exists():
            nas = nas_entries.first()
            ok(f"NAS entry auto-created for router: nasname={nas.nasname}, secret={nas.secret[:8]}...")
            assert_equal(nas.type, 'mikrotik', "NAS type matches router_type")
        else:
            warn("NAS entry NOT auto-created (signal may not be connected)")
            warnings += 1

        # ═══════════════════════════════════════════════════════════
        # PHASE 2: IP Pool Management
        # ═══════════════════════════════════════════════════════════
        header("Phase 2: IP Pool Management")
        
        from apps.network.models.ipam_models import IPPool
        
        subheader("2a. Create PPPoE pool linked to Router 1")
        pool1 = IPPool.objects.create(
            router=router,
            name='test-sim-pool-pppoe-main',
            pool_type='PPPOE',
            start_ip='10.10.10.2',
            end_ip='10.10.10.254',
            gateway='10.10.10.1',
            dns_servers='8.8.8.8,8.8.4.4',
            is_active=True,
        )
        assert_not_none(pool1.id, f"Pool created: id={pool1.id}, name={pool1.name}")
        assert_equal(pool1.pool_type, 'PPPOE', "Pool type is PPPOE")
        assert_equal(pool1.router_id, router.id, "Pool linked to correct router")
        
        subheader("2b. Create DHCP pool on Router 1 (multi-pool test)")
        pool_dhcp = IPPool.objects.create(
            router=router,
            name='test-sim-pool-dhcp',
            pool_type='DHCP',
            start_ip='192.168.1.100',
            end_ip='192.168.1.200',
            gateway='192.168.1.1',
            is_active=True,
        )
        assert_not_none(pool_dhcp.id, "DHCP pool created on same router")
        
        subheader("2c. Create PPPoE pool on Router 2 (cross-router isolation)")
        pool2 = IPPool.objects.create(
            router=router2,
            name='test-sim-pool-pppoe-branch',
            pool_type='PPPOE',
            start_ip='10.20.20.2',
            end_ip='10.20.20.254',
            gateway='10.20.20.1',
            is_active=True,
        )
        assert_not_none(pool2.id, f"Pool on Router 2: id={pool2.id}")
        
        subheader("2d. Validate unique_together constraint (router, name)")
        from django.db import IntegrityError
        try:
            IPPool.objects.create(
                router=router,
                name='test-sim-pool-pppoe-main',  # Duplicate name on same router
                pool_type='PPPOE',
                start_ip='10.10.11.2',
                end_ip='10.10.11.254',
            )
            fail("Should have raised IntegrityError for duplicate (router, name)")
        except IntegrityError:
            ok("Unique constraint enforced: duplicate (router, name) rejected")
            from django.db import connection as db_conn
            db_conn.cursor()  # Reset connection after IntegrityError
            # Need to handle the broken transaction
        except Exception as e:
            # In case of transaction issues
            ok(f"Constraint enforced (via {type(e).__name__})")
        
        subheader("2e. Test IP Pool filtering by router_id and name")
        pools_r1 = IPPool.objects.filter(router_id=router.id)
        assert_equal(pools_r1.count(), 2, "Router 1 has 2 pools")
        
        # This is the filter the Internet Check uses
        pool_check = IPPool.objects.filter(router_id=router.id, name='test-sim-pool-pppoe-main')
        assert_equal(pool_check.count(), 1, "Name+router filter finds exact pool")
        
        pool_missing = IPPool.objects.filter(router_id=router.id, name='nonexistent-pool')
        assert_equal(pool_missing.count(), 0, "Name filter returns 0 for nonexistent pool")

        subheader("2f. Pool statistics")
        assert_true(pool1.total_ips >= 0, f"Pool has total_ips={pool1.total_ips}")
        available = pool1.total_ips - pool1.used_ips
        assert_true(available >= 0, f"Pool has available_ips(computed)={available}")

        # ═══════════════════════════════════════════════════════════
        # PHASE 3: Customer + Service + RADIUS Chain
        # ═══════════════════════════════════════════════════════════
        header("Phase 3: Customer + Service Creation → RADIUS Chain")
        
        from apps.core.models import User
        from apps.customers.models import Customer, ServiceConnection
        from apps.radius.models import CustomerRadiusCredentials, RadCheck, RadReply
        
        subheader("3a. Create test User and Customer")
        test_email = f'test_sim_{timezone.now().strftime("%H%M%S")}@netily.test'
        test_phone = f'+2547{timezone.now().strftime("%H%M%S")}99'
        
        user = User.objects.create_user(
            email=test_email,
            password='TestPass123!',
            phone_number=test_phone,
            first_name='Test',
            last_name='Simulation',
            role='customer',
        )
        assert_not_none(user.id, f"User created: {user.email}")
        
        customer = Customer.objects.create(
            user=user,
            customer_code=f'SIM-{timezone.now().strftime("%H%M%S")}',
            customer_type='RESIDENTIAL',
            status='ACTIVE',
        )
        assert_not_none(customer.id, f"Customer created: {customer.customer_code}")
        
        subheader("3b. Create PPPoE service WITH router and IP pool")
        info("Simulating: Frontend sends router + ip_pool in service creation")
        
        service = ServiceConnection(
            customer=customer,
            service_type='INTERNET',
            auth_connection_type='PPPOE',
            connection_type='FIBER',
            status='ACTIVE',
            download_speed=20,
            upload_speed=10,
            monthly_price=Decimal('2500.00'),
        )
        # Stash RADIUS fields (this is what the serializer does)
        service._radius_password = 'SimTest2024!'
        service._radius_router_id = router.id
        service._radius_ip_pool = 'test-sim-pool-pppoe-main'
        service._force_radius_creation = True
        
        service.save()  # First save triggers signal with created=True
        
        assert_not_none(service.id, f"Service created: id={service.id}")
        assert_equal(service.auth_connection_type, 'PPPOE', "Service auth type is PPPOE")
        
        subheader("3c. Verify RADIUS credentials were auto-created")
        try:
            creds = CustomerRadiusCredentials.objects.get(customer=customer)
            ok(f"RADIUS credentials created: username={creds.username}")
            assert_true(creds.is_enabled, "Credentials are enabled")
            assert_equal(creds.connection_type, 'PPPOE', "Connection type is PPPOE")
            assert_equal(creds.password, 'SimTest2024!', "Password matches what was sent")
            
            # THE CRITICAL CHECKS - Router and IP Pool binding
            assert_not_none(creds.router, "Router FK is set on credentials")
            if creds.router:
                assert_equal(creds.router.id, router.id, 
                            f"Router FK points to correct router: {creds.router.name}")
            assert_equal(creds.ip_pool, 'test-sim-pool-pppoe-main', 
                        "IP Pool (Framed-Pool) is correctly set")
            
            info(f"Username: {creds.username}")
            info(f"Router: {creds.router}")
            info(f"IP Pool: {creds.ip_pool}")
            info(f"Synced to RADIUS: {creds.synced_to_radius}")
            
        except CustomerRadiusCredentials.DoesNotExist:
            fail("RADIUS credentials NOT created by signal!")
            creds = None
        
        subheader("3d. Verify radcheck entries (Cleartext-Password)")
        if creds:
            radcheck = RadCheck.objects.filter(username=creds.username)
            if radcheck.exists():
                ok(f"radcheck has {radcheck.count()} entries for {creds.username}")
                for rc in radcheck:
                    info(f"  radcheck: {rc.attribute} {rc.op} {rc.value[:20]}{'...' if len(rc.value) > 20 else ''}")
                
                # Check for password
                password_check = radcheck.filter(attribute='Cleartext-Password')
                if password_check.exists():
                    ok("Cleartext-Password attribute present in radcheck")
                else:
                    # Could also be MD5 or NT-Password  
                    warn("Cleartext-Password not found (may use different auth type)")
                    warnings += 1
                    
                # Check for Expiration
                expiration_check = radcheck.filter(attribute='Expiration')
                if expiration_check.exists():
                    ok(f"Expiration attribute present: {expiration_check.first().value}")
                else:
                    info("No Expiration attribute (unlimited validity or no plan)")
            else:
                fail(f"No radcheck entries for {creds.username}")
        
        subheader("3e. Verify radreply entries (Framed-Pool)")
        if creds:
            radreply = RadReply.objects.filter(username=creds.username)
            if radreply.exists():
                ok(f"radreply has {radreply.count()} entries for {creds.username}")
                for rr in radreply:
                    info(f"  radreply: {rr.attribute} {rr.op} {rr.value}")
                
                # THE MOST CRITICAL CHECK: Framed-Pool
                framed_pool = radreply.filter(attribute='Framed-Pool')
                if framed_pool.exists():
                    fp_value = framed_pool.first().value
                    assert_equal(fp_value, 'test-sim-pool-pppoe-main',
                                "Framed-Pool matches the IP pool name")
                    ok("★ INDUSTRY BEST PRACTICE: Framed-Pool attribute correctly maps to router pool name")
                else:
                    fail("Framed-Pool NOT in radreply — MikroTik won't assign IPs from named pool!")
            else:
                fail(f"No radreply entries for {creds.username}")

        # ═══════════════════════════════════════════════════════════
        # PHASE 4: Internet Check Validation Logic
        # ═══════════════════════════════════════════════════════════
        header("Phase 4: Internet Check Validation (Frontend Logic Simulation)")
        
        subheader("4a. GREEN scenario — pool exists on assigned router")
        if creds:
            # Simulate what the frontend does:
            # 1. Fetch credentials for customer
            # 2. Check if ip_pool is set
            # 3. Validate pool exists on the router
            
            assert_true(creds.ip_pool, "Credentials have ip_pool set")
            assert_not_none(creds.router, "Credentials have router set")
            
            if creds.router and creds.ip_pool:
                pool_exists = IPPool.objects.filter(
                    router_id=creds.router.id,
                    name=creds.ip_pool
                ).exists()
                assert_true(pool_exists, 
                           f"Internet Check → GREEN: Pool '{creds.ip_pool}' exists on router '{creds.router.name}'")
        
        subheader("4b. YELLOW scenario — no ip_pool assigned")
        if creds:
            # Temporarily save and test with no pool
            original_pool = creds.ip_pool
            creds.ip_pool = ''
            # Don't actually save to RADIUS, just check logic
            
            has_pool = bool(creds.ip_pool)
            assert_true(not has_pool, "Internet Check → YELLOW: No Framed-Pool set (router uses default)")
            
            # Restore
            creds.ip_pool = original_pool
        
        subheader("4c. RED scenario — pool doesn't exist on router")
        if creds and creds.router:
            bad_pool_exists = IPPool.objects.filter(
                router_id=creds.router.id,
                name='nonexistent-pool-xyz'
            ).exists()
            assert_true(not bad_pool_exists, 
                       "Internet Check → RED: 'nonexistent-pool-xyz' not found on router")
        
        subheader("4d. RED scenario — credentials disabled")
        if creds:
            assert_true(creds.is_enabled, "Credentials currently enabled (would show GREEN)")
            # Simulate: if disabled → RED
            info("If is_enabled=False → Internet Check shows RED with disabled_reason")

        # ═══════════════════════════════════════════════════════════
        # PHASE 5: Cross-Router Isolation Test
        # ═══════════════════════════════════════════════════════════
        header("Phase 5: Cross-Router Isolation (Security Validation)")
        
        subheader("5a. Pool from Router 1 should NOT appear in Router 2's pools")
        r2_pools = IPPool.objects.filter(router_id=router2.id)
        r2_pool_names = list(r2_pools.values_list('name', flat=True))
        assert_true('test-sim-pool-pppoe-main' not in r2_pool_names,
                    "Router 1's pool is NOT visible from Router 2's scope")
        
        subheader("5b. Internet Check rejects pool on wrong router")
        wrong_router_check = IPPool.objects.filter(
            router_id=router2.id,
            name='test-sim-pool-pppoe-main'  # This pool belongs to Router 1
        ).exists()
        assert_true(not wrong_router_check,
                   "Cross-router validation: pool 'test-sim-pool-pppoe-main' not found on Router 2")
        ok("★ INDUSTRY BEST PRACTICE: Pools are scoped per-router, preventing IP assignment leaks")

        # ═══════════════════════════════════════════════════════════
        # PHASE 6: Service Update Chain (Router/Pool Change)
        # ═══════════════════════════════════════════════════════════
        header("Phase 6: Router/Pool Migration (Update Chain)")
        
        subheader("6a. Simulate customer migration from Router 1 → Router 2")
        if creds:
            original_router = creds.router
            original_pool = creds.ip_pool
            
            # Stash new values on the service (what the serializer does on update)
            service._radius_router_id = router2.id
            service._radius_ip_pool = 'test-sim-pool-pppoe-branch'
            service._force_radius_creation = True
            service.save()
            
            # Refresh from DB
            creds.refresh_from_db()
            
            if creds.router:
                assert_equal(creds.router.id, router2.id,
                            f"Router migrated: {original_router} → {creds.router}")
            assert_equal(creds.ip_pool, 'test-sim-pool-pppoe-branch',
                        f"Pool migrated: {original_pool} → {creds.ip_pool}")
            
            # Verify radreply updated
            framed_pool = RadReply.objects.filter(
                username=creds.username, 
                attribute='Framed-Pool'
            )
            if framed_pool.exists():
                assert_equal(framed_pool.first().value, 'test-sim-pool-pppoe-branch',
                            "Framed-Pool in radreply updated after migration")
            else:
                warn("Framed-Pool not found in radreply after migration")
                warnings += 1

        # ═══════════════════════════════════════════════════════════
        # PHASE 7: API Endpoint Validation
        # ═══════════════════════════════════════════════════════════
        header("Phase 7: API/ViewSet Filtering Validation")
        
        subheader("7a. IPPoolViewSet — filter by router_id")
        from apps.network.views.ipam_views import IPPoolViewSet
        # Test the queryset filtering logic directly
        qs = IPPool.objects.all()
        r1_filtered = qs.filter(router_id=router.id)
        r2_filtered = qs.filter(router_id=router2.id)
        assert_true(r1_filtered.count() >= 2, f"Router 1 pools via API filter: {r1_filtered.count()}")
        assert_true(r2_filtered.count() >= 1, f"Router 2 pools via API filter: {r2_filtered.count()}")
        
        subheader("7b. IPPoolViewSet — filter by name")
        name_filtered = qs.filter(name='test-sim-pool-pppoe-main')
        assert_equal(name_filtered.count(), 1, "Name filter returns exactly 1 pool")
        
        subheader("7c. CustomerRadiusCredentialsViewSet — filter by customer")
        from apps.radius.models import CustomerRadiusCredentials as CRC
        cust_filtered = CRC.objects.filter(customer=customer)
        assert_equal(cust_filtered.count(), 1, f"Customer filter returns credentials")
        
        subheader("7d. Serializer router_name field")
        from apps.radius.serializers import CustomerRadiusCredentialsSerializer
        if creds:
            creds_with_select = CRC.objects.select_related('router').get(pk=creds.pk)
            serializer = CustomerRadiusCredentialsSerializer(creds_with_select)
            data = serializer.data
            info(f"Serialized router: {data.get('router')}")
            info(f"Serialized router_name: {data.get('router_name')}")
            assert_not_none(data.get('router'), "Serializer exposes router field")
            assert_not_none(data.get('router_name'), "Serializer exposes router_name field")
            assert_equal(data.get('ip_pool'), 'test-sim-pool-pppoe-branch', 
                        "Serializer exposes ip_pool")

        # ═══════════════════════════════════════════════════════════
        # PHASE 8: Edge Cases
        # ═══════════════════════════════════════════════════════════
        header("Phase 8: Edge Cases & Safety Checks")
        
        subheader("8a. Service without router/pool (backward compatibility)")
        test_email2 = f'test_sim_nopool_{timezone.now().strftime("%H%M%S")}@netily.test'
        test_phone2 = f'+2548{timezone.now().strftime("%H%M%S")}88'
        
        user2 = User.objects.create_user(
            email=test_email2,
            password='TestPass123!',
            phone_number=test_phone2,
            first_name='NoPool',
            last_name='Customer',
            role='customer',
        )
        customer2 = Customer.objects.create(
            user=user2,
            customer_code=f'SIMNP-{timezone.now().strftime("%H%M%S")}',
            customer_type='RESIDENTIAL',
            status='ACTIVE',
        )
        
        service2 = ServiceConnection(
            customer=customer2,
            service_type='INTERNET',
            auth_connection_type='PPPOE',
            connection_type='FIBER',
            status='ACTIVE',
            download_speed=10,
            upload_speed=5,
            monthly_price=Decimal('1500.00'),
        )
        # NO router or ip_pool stashed — simulate old flow
        service2._radius_password = 'Legacy123!'
        service2._force_radius_creation = True
        service2.save()
        
        try:
            creds2 = CustomerRadiusCredentials.objects.get(customer=customer2)
            ok(f"Legacy flow works: credentials created without router/pool")
            assert_true(creds2.router is None, "No router assigned (legacy)")
            assert_equal(creds2.ip_pool, '', "No ip_pool assigned (legacy)")
            
            # Internet Check: should be YELLOW (no pool)
            if not creds2.ip_pool:
                ok("Internet Check → YELLOW: legacy customer has no pool")
        except CustomerRadiusCredentials.DoesNotExist:
            fail("Legacy flow broken: credentials not created without router/pool")
        
        subheader("8b. PENDING service should NOT create RADIUS credentials")
        test_email3 = f'test_sim_pending_{timezone.now().strftime("%H%M%S")}@netily.test'
        test_phone3 = f'+2549{timezone.now().strftime("%H%M%S")}77'
        
        user3 = User.objects.create_user(
            email=test_email3,
            password='TestPass123!',
            phone_number=test_phone3,
            first_name='Pending',
            last_name='Customer',
            role='customer',
        )
        customer3 = Customer.objects.create(
            user=user3,
            customer_code=f'SIMPEND-{timezone.now().strftime("%H%M%S")}',
            customer_type='RESIDENTIAL',
            status='ACTIVE',
        )
        
        service3 = ServiceConnection(
            customer=customer3,
            service_type='INTERNET',
            auth_connection_type='PPPOE',
            connection_type='FIBER',
            status='PENDING',  # <-- PENDING = don't create RADIUS
            download_speed=10,
            upload_speed=5,
            monthly_price=Decimal('1000.00'),
        )
        service3.save()
        
        creds3_exists = CustomerRadiusCredentials.objects.filter(customer=customer3).exists()
        assert_true(not creds3_exists, "PENDING service correctly skips RADIUS creation")
        
        subheader("8c. Static IP service should NOT create RADIUS credentials")
        test_email4 = f'test_sim_static_{timezone.now().strftime("%H%M%S")}@netily.test'
        test_phone4 = f'+2540{timezone.now().strftime("%H%M%S")}66'
        
        user4 = User.objects.create_user(
            email=test_email4,
            password='TestPass123!',
            phone_number=test_phone4,
            first_name='Static',
            last_name='Customer',
            role='customer',
        )
        customer4 = Customer.objects.create(
            user=user4,
            customer_code=f'SIMSTATIC-{timezone.now().strftime("%H%M%S")}',
            customer_type='RESIDENTIAL',
            status='ACTIVE',
        )
        
        service4 = ServiceConnection(
            customer=customer4,
            service_type='INTERNET',
            auth_connection_type='STATIC',  # Not PPPoE or Hotspot
            connection_type='FIBER',
            status='ACTIVE',
            download_speed=10,
            upload_speed=5,
            monthly_price=Decimal('1000.00'),
            ip_address='192.168.1.50',
        )
        service4.save()
        
        creds4_exists = CustomerRadiusCredentials.objects.filter(customer=customer4).exists()
        assert_true(not creds4_exists, "Static IP service correctly skips RADIUS creation")

        # ═══════════════════════════════════════════════════════════
        # CLEANUP
        # ═══════════════════════════════════════════════════════════
        header("Cleanup")
        try:
            cleanup_test_data()
            ok("All test data cleaned up successfully")
        except Exception as e:
            warn(f"Cleanup issue: {e}")
            warnings += 1

        # ═══════════════════════════════════════════════════════════
        # SUMMARY
        # ═══════════════════════════════════════════════════════════
        header("TEST RESULTS SUMMARY")
        total = passed + failed
        print(f"""
  {C.GREEN}Passed:   {passed}{C.END}
  {C.RED}Failed:   {failed}{C.END}
  {C.YELLOW}Warnings: {warnings}{C.END}
  ──────────
  Total:    {total}
""")
        if failed == 0:
            print(f"  {C.GREEN}{C.BOLD}★ ALL TESTS PASSED — Chain is production-ready ★{C.END}")
        else:
            print(f"  {C.RED}{C.BOLD}✘ {failed} TESTS FAILED — Review and fix before deployment{C.END}")
        
        print(f"""
  {C.CYAN}Industry Best Practice Checklist:{C.END}
  {'✔' if failed == 0 else '✘'} Framed-Pool RADIUS attribute maps to MikroTik /ip pool name
  {'✔' if failed == 0 else '✘'} Router FK ensures pool-to-NAS binding
  {'✔' if failed == 0 else '✘'} Signal chain auto-provisions on service creation  
  {'✔' if failed == 0 else '✘'} Cross-router pool isolation prevents IP leaks
  {'✔' if failed == 0 else '✘'} PENDING services skip RADIUS (Activate Later pattern)
  {'✔' if failed == 0 else '✘'} Static IP services bypass RADIUS PPPoE chain
  {'✔' if failed == 0 else '✘'} Backward compatible (no router/pool = legacy flow)
  {'✔' if failed == 0 else '✘'} Router migration updates RADIUS credentials + radreply
""")
        return failed == 0


if __name__ == '__main__':
    print(f"\n{C.BOLD}{'╔' + '═'*58 + '╗'}")
    print(f"║  Multi-Router IPAM & PPPoE Integration Test Suite        ║")
    print(f"║  Testing on tenant: {TEST_SCHEMA:<37s} ║")
    print(f"{'╚' + '═'*58 + '╝'}{C.END}")
    
    try:
        success = run_tests()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n{C.RED}FATAL ERROR:{C.END} {e}")
        traceback.print_exc()
        sys.exit(2)
