"""Create a test tenant and run full API tests"""
import os, sys, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')
sys.path.insert(0, '/app')
django.setup()

from apps.core.models import Tenant, Domain, Company
from django.db import connection
from datetime import date, timedelta

print("=" * 60)
print("CREATING TEST TENANT")
print("=" * 60)

# Create Company first
company, created = Company.objects.get_or_create(
    slug='test-isp',
    defaults={
        'name': 'Test ISP Ltd',
        'company_type': 'isp',
        'email': 'admin@testisp.com',
        'phone_number': '+254700000000',
        'address': '123 Test St',
        'city': 'Nairobi',
        'subscription_plan': 'enterprise',
        'subscription_expiry': date.today() + timedelta(days=365),
    }
)
if created:
    print("[PASS] Created company 'Test ISP Ltd'")
else:
    print("[PASS] Company 'Test ISP Ltd' exists")

# Create public tenant first (required by django-tenants)
try:
    public_tenant = Tenant.objects.get(schema_name='public')
    print("[PASS] Public tenant exists")
except Tenant.DoesNotExist:
    # Need a separate company for public
    pub_company, _ = Company.objects.get_or_create(
        slug='public',
        defaults={
            'name': 'Netily Platform',
            'company_type': 'isp',
            'email': 'admin@netily.com',
            'phone_number': '+254700000001',
            'address': 'Nairobi',
            'city': 'Nairobi',
            'subscription_plan': 'enterprise',
            'subscription_expiry': date.today() + timedelta(days=365),
        }
    )
    public_tenant = Tenant(
        schema_name='public',
        subdomain='public',
        domain='localhost',
        status='active',
        company=pub_company,
        trial_start=date.today(),
        trial_days=365,
        subscription_expiry=date.today() + timedelta(days=365),
        max_users=100,
        max_customers=1000,
        next_billing_date=date.today() + timedelta(days=30),
    )
    public_tenant.save()
    Domain.objects.create(domain='localhost', tenant=public_tenant, is_primary=True)
    print("[PASS] Created public tenant")

# Create test ISP tenant
try:
    test_tenant = Tenant.objects.get(schema_name='test_isp')
    print("[PASS] Test tenant 'test_isp' exists")
except Tenant.DoesNotExist:
    test_tenant = Tenant(
        schema_name='test_isp',
        subdomain='test',
        domain='test.localhost',
        status='active',
        company=company,
        trial_start=date.today(),
        trial_days=365,
        subscription_expiry=date.today() + timedelta(days=365),
        max_users=100,
        max_customers=1000,
        next_billing_date=date.today() + timedelta(days=30),
    )
    test_tenant.save()  # This creates the schema and runs tenant migrations
    Domain.objects.create(
        domain='test.localhost',
        tenant=test_tenant,
        is_primary=True
    )
    print("[PASS] Created test tenant 'test_isp' (schema created + migrated)")

# Verify the network_router table now exists
cursor = connection.cursor()
cursor.execute("""
    SELECT table_schema, table_name 
    FROM information_schema.tables 
    WHERE table_name = 'network_router'
    ORDER BY table_schema
""")
rows = cursor.fetchall()
print()
print("=== network_router table locations ===")
for row in rows:
    print("  schema: {}, table: {}".format(row[0], row[1]))

print()
print("Tenant schema: {}".format(test_tenant.schema_name))
print("Done!")
