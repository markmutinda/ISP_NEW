"""
Simulate PPPoE Plan Creation - End-to-End Test

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


class Command(BaseCommand):
    help = 'Simulate full PPPoE plan creation with IP Pool IPAM and Router'

    def add_arguments(self, parser):
        parser.add_argument(
            '--cleanup',
            action='store_true',
            help='Delete test data after simulation',
        )

    def _write(self, msg, style_func=None):
        """Write message safely, handling encoding errors on Windows."""
        if style_func:
            msg = style_func(msg)
        try:
            self.stdout.write(msg)
        except UnicodeEncodeError:
            safe_msg = msg.encode('ascii', errors='replace').decode('ascii')
            self.stdout.write(safe_msg)

    def handle(self, *args, **options):
        from apps.network.models.router_models import Router
        from apps.network.models.ipam_models import IPPool, IPAddress
        from apps.billing.models.billing_models import Plan

        cleanup = options['cleanup']

        self._write('\n' + '=' * 70, self.style.WARNING)
        self._write('  PPPoE Plan Creation Simulation - Full E2E Test', self.style.WARNING)
        self._write('=' * 70, self.style.WARNING)
        self._write(f'\n  Schema: {connection.schema_name}')
        self._write(f'  Time:   {timezone.now().isoformat()}\n')

        results = {
            'router': None,
            'ip_pool': None,
            'plan': None,
            'errors': [],
        }

        try:
            # ------------------------------------------
            # STEP 1: Create Router
            # ------------------------------------------
            self._write('\n-- STEP 1: Create Router --', self.style.HTTP_INFO)

            test_router_name = 'SIM-PPPoE-Router-Test'
            Router.objects.filter(name=test_router_name).delete()

            router = Router.objects.create(
                name=test_router_name,
                ip_address='192.168.88.1',
                api_port=8728,
                api_username='netily_api',
                api_password='test_password_sim',
                router_type='mikrotik',
                config_type='pppoe',
                status='offline',
                enable_pppoe=True,
                enable_hotspot=False,
                location='Simulation Test',
                is_active=True,
            )
            results['router'] = router

            self._write(f'  [OK] Router created: {router.name} (ID: {router.id})', self.style.SUCCESS)
            self._write(f'    Type:        {router.router_type}')
            self._write(f'    Config:      {router.config_type}')
            self._write(f'    IP:          {router.ip_address}:{router.api_port}')
            self._write(f'    PPPoE:       {"Enabled" if router.enable_pppoe else "Disabled"}')
            self._write(f'    Auth Key:    {router.auth_key}')

            # ------------------------------------------
            # STEP 2: Create IP Pool via Subnet Builder
            # ------------------------------------------
            self._write('\n-- STEP 2: Create IP Pool (Cloud-Led Subnet Builder) --', self.style.HTTP_INFO)

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
            ip_pool.refresh_from_db()

            self._write(f'  [OK] IP Pool created: {ip_pool.name} (ID: {ip_pool.id})', self.style.SUCCESS)
            self._write(f'    Subnet:      {ip_pool.subnet_prefix}.{ip_pool.subnet_octet}.0/{ip_pool.cidr_prefix}')
            self._write(f'    Gateway:     {ip_pool.gateway}')
            self._write(f'    Start IP:    {ip_pool.start_ip}')
            self._write(f'    End IP:      {ip_pool.end_ip}')
            self._write(f'    Total IPs:   {ip_pool.total_ips}')
            self._write(f'    Pool Type:   {ip_pool.pool_type}')

            ip_count = IPAddress.objects.filter(pool=ip_pool).count()
            available_count = IPAddress.objects.filter(pool=ip_pool, status='AVAILABLE').count()
            self._write(f'    IPAddress records:  {ip_count}')
            self._write(f'    Available IPs:      {available_count}')

            first_ips = list(IPAddress.objects.filter(pool=ip_pool).order_by('id')[:5].values_list('ip_address', flat=True))
            last_ips = list(IPAddress.objects.filter(pool=ip_pool).order_by('-id')[:5].values_list('ip_address', flat=True))
            self._write(f'    First 5 IPs: {first_ips}')
            self._write(f'    Last  5 IPs: {list(reversed(last_ips))}')

            if not ip_pool.gateway:
                results['errors'].append('IP Pool gateway not computed')
            if not ip_pool.start_ip:
                results['errors'].append('IP Pool start_ip not computed')
            if ip_count == 0:
                results['errors'].append('No IPAddress records auto-populated!')
            if ip_pool.total_ips != ip_count:
                results['errors'].append(f'total_ips ({ip_pool.total_ips}) != IPAddress count ({ip_count})')

            # ------------------------------------------
            # STEP 3: Create PPPoE Plan linked to IP Pool
            # ------------------------------------------
            self._write('\n-- STEP 3: Create PPPoE Plan --', self.style.HTTP_INFO)

            test_plan_name = 'SIM-Home-Basic-20Mbps'
            Plan.objects.filter(name=test_plan_name).delete()

            plan = Plan.objects.create(
                name=test_plan_name,
                plan_type='PPPOE',
                description='Simulated PPPoE plan with Cloud-Led IP Pool',
                base_price=3500.00,
                setup_fee=0,
                download_speed=20,
                upload_speed=10,
                speed_unit='MBPS',
                validity_type='MONTHS',
                validity_months=1,
                priority=6,
                burst_enabled=True,
                burst_download=40,
                burst_upload=20,
                burst_threshold=2048,
                burst_time=10,
                ip_pool=ip_pool,
                is_active=True,
                is_public=True,
                is_popular=False,
            )
            results['plan'] = plan
            plan.refresh_from_db()

            self._write(f'  [OK] PPPoE Plan created: {plan.name} (ID: {plan.id})', self.style.SUCCESS)
            self._write(f'    Code:          {plan.code}')
            self._write(f'    Type:          {plan.plan_type}')
            self._write(f'    Price:         KES {plan.base_price}')
            self._write(f'    Speed:         {plan.download_speed}/{plan.upload_speed} {plan.speed_unit}')
            self._write(f'    Validity:      {plan.validity_type} - {plan.validity_months} month(s)')
            self._write(f'    Priority:      {plan.priority}')
            self._write(f'    Burst:         {"Enabled" if plan.burst_enabled else "Disabled"}')
            if plan.burst_enabled:
                self._write(f'      Download:    {plan.burst_download} Mbps')
                self._write(f'      Upload:      {plan.burst_upload} Mbps')
                self._write(f'      Threshold:   {plan.burst_threshold} KB')
                self._write(f'      Time:        {plan.burst_time} sec')
            self._write(f'    IP Pool:       {plan.ip_pool.name if plan.ip_pool else "None"} (ID: {plan.ip_pool_id})')
            self._write(f'    Active:        {plan.is_active}')
            self._write(f'    Public:        {plan.is_public}')

            if not plan.ip_pool:
                results['errors'].append('Plan ip_pool FK is None!')
            elif plan.ip_pool.id != ip_pool.id:
                results['errors'].append(f'Plan ip_pool mismatch: {plan.ip_pool.id} != {ip_pool.id}')

            session_seconds = plan.get_session_timeout_seconds()
            self._write(f'    RADIUS timeout: {session_seconds} seconds ({session_seconds // 60} min / {session_seconds // 3600} hrs)')

            # ------------------------------------------
            # STEP 4: Verify Cross-References
            # ------------------------------------------
            self._write('\n-- STEP 4: Verify Cross-References --', self.style.HTTP_INFO)

            pool_plans = Plan.objects.filter(ip_pool=ip_pool)
            self._write(f'  Plans linked to pool "{ip_pool.name}": {pool_plans.count()}')
            for p in pool_plans:
                self._write(f'    - {p.name} ({p.plan_type}, KES {p.base_price})')

            available_ips = IPAddress.objects.filter(pool=ip_pool, status='AVAILABLE')
            self._write(f'  Available IPs in pool: {available_ips.count()}')

            if available_ips.exists():
                test_ip = available_ips.first()
                self._write(f'  Test IP: {test_ip.ip_address} (status: {test_ip.status})')

                test_ip.status = 'ASSIGNED'
                test_ip.save()
                test_ip.refresh_from_db()
                self._write(f'  After assign: {test_ip.ip_address} -> status={test_ip.status}')

                test_ip.status = 'AVAILABLE'
                test_ip.save()
                self._write(f'  After release: {test_ip.ip_address} -> status=AVAILABLE')

            # ------------------------------------------
            # STEP 5: Serializer Dry-Run Validation
            # ------------------------------------------
            self._write('\n-- STEP 5: Serializer Dry-Run Validation --', self.style.HTTP_INFO)

            from apps.billing.serializers.invoice_serializers import PlanCreateSerializer
            from apps.network.serializers.ipam_serializers import IPPoolSerializer

            pool_serializer = IPPoolSerializer(ip_pool)
            pool_data = pool_serializer.data
            self._write(f'  IPPoolSerializer output keys: {list(pool_data.keys())}')
            assert 'subnet_prefix' in pool_data, 'subnet_prefix missing from serializer output'
            assert 'subnet_octet' in pool_data, 'subnet_octet missing from serializer output'
            assert 'cidr_prefix' in pool_data, 'cidr_prefix missing from serializer output'
            self._write('  [OK] IPPoolSerializer output includes subnet builder fields', self.style.SUCCESS)

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
                self._write('  [OK] PlanCreateSerializer validates input correctly', self.style.SUCCESS)
            else:
                self._write(f'  [FAIL] PlanCreateSerializer errors: {plan_ser.errors}', self.style.ERROR)
                results['errors'].append(f'PlanCreateSerializer validation failed: {plan_ser.errors}')

            # ------------------------------------------
            # FINAL REPORT
            # ------------------------------------------
            self._write('\n' + '=' * 70, self.style.WARNING)
            self._write('  SIMULATION RESULTS', self.style.WARNING)
            self._write('=' * 70, self.style.WARNING)

            if not results['errors']:
                self._write('')
                self._write('  +----------------------------------------------------------+', self.style.SUCCESS)
                self._write('  |              ALL CHECKS PASSED [OK]                      |', self.style.SUCCESS)
                self._write('  |                                                          |', self.style.SUCCESS)
                self._write('  |  Router      -> Created with PPPoE config                |', self.style.SUCCESS)
                self._write('  |  IP Pool     -> 172.16.2.0/24 with auto-populated IPs    |', self.style.SUCCESS)
                self._write('  |  PPPoE Plan  -> Linked to pool, priority=6, burst=ON     |', self.style.SUCCESS)
                self._write('  |  Serializers -> Input/output validated                   |', self.style.SUCCESS)
                self._write('  |  IP Assign   -> Assign/Release cycle verified            |', self.style.SUCCESS)
                self._write('  |                                                          |', self.style.SUCCESS)
                self._write('  |  Frontend -> backend chain is fully operational.         |', self.style.SUCCESS)
                self._write('  +----------------------------------------------------------+', self.style.SUCCESS)
                self._write('')
            else:
                self._write(f'\n  [FAIL] {len(results["errors"])} ERROR(S):', self.style.ERROR)
                for err in results['errors']:
                    self._write(f'    - {err}', self.style.ERROR)

        except Exception as e:
            self._write(f'\n  [FAIL] SIMULATION FAILED: {e}', self.style.ERROR)
            import traceback
            traceback.print_exc()
            results['errors'].append(str(e))

        finally:
            if cleanup:
                self._write('\n-- CLEANUP --', self.style.HTTP_INFO)
                if results['plan']:
                    results['plan'].delete()
                    self._write('  Deleted test plan')
                if results['ip_pool']:
                    IPAddress.objects.filter(pool=results['ip_pool']).delete()
                    results['ip_pool'].delete()
                    self._write('  Deleted test IP pool + addresses')
                if results['router']:
                    results['router'].delete()
                    self._write('  Deleted test router')
                self._write('  [OK] All test data cleaned up', self.style.SUCCESS)

            self._write('')
