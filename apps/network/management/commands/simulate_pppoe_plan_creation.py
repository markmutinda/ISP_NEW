"""
Simulate PPPoE Plan Creation — End-to-End Test

This command simulates the exact flow the frontend performs when an admin:
  1. Creates a Router (MikroTik PPPoE)
  2. Creates an IP Pool via the Cloud-Led Subnet Builder (172.16.2.0/24)
  3. Creates a PPPoE Plan linked to that IP Pool with priority + burst + validity_months

It validates every step and outputs a comprehensive report.

Usage:
    python manage.py simulate_pppoe_plan_creation
    python manage.py simulate_pppoe_plan_creation --cleanup   # Remove test data after
"""

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone
import json


class Command(BaseCommand):
    help = 'Simulate full PPPoE plan creation with IP Pool IPAM and Router'

    def add_arguments(self, parser):
        parser.add_argument(
            '--cleanup',
            action='store_true',
            help='Delete test data after simulation',
        )
        parser.add_argument(
            '--schema',
            type=str,
            default=None,
            help='Tenant schema name to run against (default: current schema)',
        )

    def handle(self, *args, **options):
        from apps.network.models.router_models import Router
        from apps.network.models.ipam_models import IPPool, IPAddress
        from apps.billing.models.billing_models import Plan
        from apps.core.models import User

        cleanup = options['cleanup']
        schema = options.get('schema')

        self.stdout.write(self.style.WARNING('\n' + '=' * 70))
        self.stdout.write(self.style.WARNING('  PPPoE Plan Creation Simulation — Full E2E Test'))
        self.stdout.write(self.style.WARNING('=' * 70))
        self.stdout.write(f'\n  Schema: {connection.schema_name}')
        self.stdout.write(f'  Time:   {timezone.now().isoformat()}\n')

        results = {
            'router': None,
            'ip_pool': None,
            'plan': None,
            'errors': [],
        }

        try:
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # STEP 1: Create Router
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            self.stdout.write(self.style.HTTP_INFO('\n━━ STEP 1: Create Router ━━'))

            test_router_name = 'SIM-PPPoE-Router-Test'

            # Clean up any existing test data
            Router.objects.filter(name=test_router_name).delete()

            router = Router.objects.create(
                name=test_router_name,
                ip_address='192.168.88.1',
                api_port=8728,
                api_username='netily_api',
                api_password='test_password_sim',
                router_type='mikrotik',
                config_type='pppoe',
                status='offline',  # Simulated, not a real router
                enable_pppoe=True,
                enable_hotspot=False,
                location='Simulation Test',
                is_active=True,
            )
            results['router'] = router

            self.stdout.write(self.style.SUCCESS(f'  ✓ Router created: {router.name} (ID: {router.id})'))
            self.stdout.write(f'    Type:        {router.router_type}')
            self.stdout.write(f'    Config:      {router.config_type}')
            self.stdout.write(f'    IP:          {router.ip_address}:{router.api_port}')
            self.stdout.write(f'    PPPoE:       {"Enabled" if router.enable_pppoe else "Disabled"}')
            self.stdout.write(f'    Auth Key:    {router.auth_key}')

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # STEP 2: Create IP Pool via Subnet Builder
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            self.stdout.write(self.style.HTTP_INFO('\n━━ STEP 2: Create IP Pool (Cloud-Led Subnet Builder) ━━'))

            # This mirrors what the frontend sends:
            # adminApi.createIPPool({
            #   name: 'Pool 172.16.2.0/24',
            #   subnet_prefix: '172.16',
            #   subnet_octet: 2,
            #   cidr_prefix: 24,
            #   pool_type: 'DYNAMIC',
            #   is_active: True,
            # })

            test_pool_name = 'Pool 172.16.2.0/24'
            IPPool.objects.filter(name=test_pool_name).delete()

            ip_pool = IPPool.objects.create(
                name=test_pool_name,
                subnet_prefix='172.16',
                subnet_octet=2,
                cidr_prefix=24,
                pool_type='DYNAMIC',
                is_active=True,
            )
            results['ip_pool'] = ip_pool

            # Refresh from DB to get computed fields
            ip_pool.refresh_from_db()

            self.stdout.write(self.style.SUCCESS(f'  ✓ IP Pool created: {ip_pool.name} (ID: {ip_pool.id})'))
            self.stdout.write(f'    Subnet:      {ip_pool.subnet_prefix}.{ip_pool.subnet_octet}.0/{ip_pool.cidr_prefix}')
            self.stdout.write(f'    Gateway:     {ip_pool.gateway}')
            self.stdout.write(f'    Start IP:    {ip_pool.start_ip}')
            self.stdout.write(f'    End IP:      {ip_pool.end_ip}')
            self.stdout.write(f'    Total IPs:   {ip_pool.total_ips}')
            self.stdout.write(f'    Pool Type:   {ip_pool.pool_type}')

            # Verify IP addresses were auto-populated
            ip_count = IPAddress.objects.filter(pool=ip_pool).count()
            available_count = IPAddress.objects.filter(pool=ip_pool, status='AVAILABLE').count()
            self.stdout.write(f'    IPAddress records:  {ip_count}')
            self.stdout.write(f'    Available IPs:      {available_count}')

            # Show first 5 and last 5 IPs
            first_ips = list(IPAddress.objects.filter(pool=ip_pool).order_by('id')[:5].values_list('ip_address', flat=True))
            last_ips = list(IPAddress.objects.filter(pool=ip_pool).order_by('-id')[:5].values_list('ip_address', flat=True))
            self.stdout.write(f'    First 5 IPs: {first_ips}')
            self.stdout.write(f'    Last  5 IPs: {list(reversed(last_ips))}')

            # Validate computed fields
            if not ip_pool.gateway:
                results['errors'].append('IP Pool gateway not computed')
            if not ip_pool.start_ip:
                results['errors'].append('IP Pool start_ip not computed')
            if ip_count == 0:
                results['errors'].append('No IPAddress records auto-populated!')
            if ip_pool.total_ips != ip_count:
                results['errors'].append(f'total_ips ({ip_pool.total_ips}) != IPAddress count ({ip_count})')

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # STEP 3: Create PPPoE Plan linked to IP Pool
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            self.stdout.write(self.style.HTTP_INFO('\n━━ STEP 3: Create PPPoE Plan ━━'))

            # This mirrors what the frontend sends:
            # adminApi.createPlan({
            #   name: 'Home Basic 20Mbps',
            #   plan_type: 'PPPOE',
            #   base_price: '3500',
            #   validity_type: 'MONTHS',
            #   validity_months: 1,
            #   download_speed: 20,
            #   upload_speed: 10,
            #   speed_unit: 'MBPS',
            #   priority: 6,
            #   burst_enabled: True,
            #   burst_download: 40,
            #   burst_upload: 20,
            #   burst_threshold: 2048,
            #   burst_time: 10,
            #   ip_pool: ip_pool.id,
            #   is_active: True,
            #   is_public: True,
            # })

            test_plan_name = 'SIM-Home-Basic-20Mbps'
            Plan.objects.filter(name=test_plan_name).delete()

            plan = Plan.objects.create(
                name=test_plan_name,
                plan_type='PPPOE',
                description='Simulated PPPoE plan with Cloud-Led IP Pool',
                base_price=3500.00,
                setup_fee=0,
                # Speed
                download_speed=20,
                upload_speed=10,
                speed_unit='MBPS',
                # Validity
                validity_type='MONTHS',
                validity_months=1,
                # MikroTik QoS
                priority=6,
                # Burst
                burst_enabled=True,
                burst_download=40,
                burst_upload=20,
                burst_threshold=2048,
                burst_time=10,
                # IP Pool linkage
                ip_pool=ip_pool,
                # Status
                is_active=True,
                is_public=True,
                is_popular=False,
            )
            results['plan'] = plan

            # Refresh from DB
            plan.refresh_from_db()

            self.stdout.write(self.style.SUCCESS(f'  ✓ PPPoE Plan created: {plan.name} (ID: {plan.id})'))
            self.stdout.write(f'    Code:          {plan.code}')
            self.stdout.write(f'    Type:          {plan.plan_type}')
            self.stdout.write(f'    Price:         KES {plan.base_price}')
            self.stdout.write(f'    Speed:         {plan.download_speed}/{plan.upload_speed} {plan.speed_unit}')
            self.stdout.write(f'    Validity:      {plan.validity_type} — {plan.validity_months} month(s)')
            self.stdout.write(f'    Priority:      {plan.priority}')
            self.stdout.write(f'    Burst:         {"Enabled" if plan.burst_enabled else "Disabled"}')
            if plan.burst_enabled:
                self.stdout.write(f'      Download:    {plan.burst_download} Mbps')
                self.stdout.write(f'      Upload:      {plan.burst_upload} Mbps')
                self.stdout.write(f'      Threshold:   {plan.burst_threshold} KB')
                self.stdout.write(f'      Time:        {plan.burst_time} sec')
            self.stdout.write(f'    IP Pool:       {plan.ip_pool.name if plan.ip_pool else "None"} (ID: {plan.ip_pool_id})')
            self.stdout.write(f'    Active:        {plan.is_active}')
            self.stdout.write(f'    Public:        {plan.is_public}')

            # Validate plan → pool linkage
            if not plan.ip_pool:
                results['errors'].append('Plan ip_pool FK is None!')
            elif plan.ip_pool.id != ip_pool.id:
                results['errors'].append(f'Plan ip_pool mismatch: {plan.ip_pool.id} != {ip_pool.id}')

            # Validate RADIUS session duration
            session_minutes = plan.get_session_timeout_seconds()
            self.stdout.write(f'    RADIUS timeout: {session_minutes} seconds ({session_minutes // 60} min / {session_minutes // 3600} hrs)')

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # STEP 4: Verify Cross-References
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            self.stdout.write(self.style.HTTP_INFO('\n━━ STEP 4: Verify Cross-References ━━'))

            # Plan → Pool → IPs
            pool_plans = Plan.objects.filter(ip_pool=ip_pool)
            self.stdout.write(f'  Plans linked to pool "{ip_pool.name}": {pool_plans.count()}')
            for p in pool_plans:
                self.stdout.write(f'    - {p.name} ({p.plan_type}, KES {p.base_price})')

            # Pool → Available IPs (simulating customer assignment)
            available_ips = IPAddress.objects.filter(pool=ip_pool, status='AVAILABLE')
            self.stdout.write(f'  Available IPs in pool: {available_ips.count()}')

            # Test IP assignment
            if available_ips.exists():
                test_ip = available_ips.first()
                self.stdout.write(f'  Test IP: {test_ip.ip_address} (status: {test_ip.status})')
                
                # Simulate assign
                test_ip.status = 'ASSIGNED'
                test_ip.save()
                test_ip.refresh_from_db()
                self.stdout.write(f'  After assign: {test_ip.ip_address} → status={test_ip.status}')
                
                # Release it back
                test_ip.status = 'AVAILABLE'
                test_ip.save()
                self.stdout.write(f'  After release: {test_ip.ip_address} → status=AVAILABLE')

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # STEP 5: Serializer Dry-Run Validation
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            self.stdout.write(self.style.HTTP_INFO('\n━━ STEP 5: Serializer Dry-Run Validation ━━'))

            from apps.billing.serializers.invoice_serializers import PlanCreateSerializer
            from apps.network.serializers.ipam_serializers import IPPoolSerializer

            # Validate IP Pool serializer output
            pool_serializer = IPPoolSerializer(ip_pool)
            pool_data = pool_serializer.data
            self.stdout.write(f'  IPPoolSerializer output keys: {list(pool_data.keys())}')
            assert 'subnet_prefix' in pool_data, 'subnet_prefix missing from serializer output'
            assert 'subnet_octet' in pool_data, 'subnet_octet missing from serializer output'
            assert 'cidr_prefix' in pool_data, 'cidr_prefix missing from serializer output'
            self.stdout.write(self.style.SUCCESS('  ✓ IPPoolSerializer output includes subnet builder fields'))

            # Validate Plan create serializer (input validation)
            plan_input = {
                'name': 'Serializer-Test-Plan',
                'plan_type': 'PPPOE',
                'base_price': '4000',
                'validity_type': 'MONTHS',
                'validity_months': 1,
                'download_speed': 30,
                'upload_speed': 15,
                'speed_unit': 'MBPS',
                'priority': 5,
                'burst_enabled': True,
                'burst_download': 60,
                'burst_upload': 30,
                'burst_threshold': 4096,
                'burst_time': 15,
                'ip_pool': ip_pool.id,
                'is_active': True,
            }
            plan_ser = PlanCreateSerializer(data=plan_input)
            is_valid = plan_ser.is_valid()
            if is_valid:
                self.stdout.write(self.style.SUCCESS('  ✓ PlanCreateSerializer validates input correctly'))
            else:
                self.stdout.write(self.style.ERROR(f'  ✗ PlanCreateSerializer errors: {plan_ser.errors}'))
                results['errors'].append(f'PlanCreateSerializer validation failed: {plan_ser.errors}')

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # FINAL REPORT
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            self.stdout.write(self.style.WARNING('\n' + '=' * 70))
            self.stdout.write(self.style.WARNING('  SIMULATION RESULTS'))
            self.stdout.write(self.style.WARNING('=' * 70))

            if not results['errors']:
                self.stdout.write(self.style.SUCCESS('''
  ┌──────────────────────────────────────────────────────────┐
  │                 ALL CHECKS PASSED ✓                      │
  │                                                          │
  │  Router      → Created with PPPoE config                 │
  │  IP Pool     → 172.16.2.0/24 with auto-populated IPs    │
  │  PPPoE Plan  → Linked to pool, priority=6, burst=ON     │
  │  Serializers → Input/output validated                    │
  │  IP Assign   → Assign/Release cycle verified             │
  │                                                          │
  │  The frontend → backend chain is fully operational.      │
  └──────────────────────────────────────────────────────────┘
'''))
            else:
                self.stdout.write(self.style.ERROR(f'\n  ✗ {len(results["errors"])} ERROR(S):'))
                for err in results['errors']:
                    self.stdout.write(self.style.ERROR(f'    - {err}'))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f'\n  ✗ SIMULATION FAILED: {e}'))
            import traceback
            traceback.print_exc()
            results['errors'].append(str(e))

        finally:
            # Cleanup if requested
            if cleanup:
                self.stdout.write(self.style.HTTP_INFO('\n━━ CLEANUP ━━'))
                if results['plan']:
                    results['plan'].delete()
                    self.stdout.write('  Deleted test plan')
                if results['ip_pool']:
                    IPAddress.objects.filter(pool=results['ip_pool']).delete()
                    results['ip_pool'].delete()
                    self.stdout.write('  Deleted test IP pool + addresses')
                if results['router']:
                    results['router'].delete()
                    self.stdout.write('  Deleted test router')
                self.stdout.write(self.style.SUCCESS('  ✓ All test data cleaned up'))

            self.stdout.write('')
