import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from apps.core.models import Company, Tenant
from django.utils import timezone

print("=== CREATING TEST TENANT ===")

# Create a test company
company, created = Company.objects.get_or_create(
    name="BlueNet ISP",
    defaults={
        'slug': 'bluenet-isp',
        'company_type': 'isp',
        'email': 'info@bluenet.co.ke',
        'phone_number': '+254720123456',
        'address': '123 Main Street, Nairobi',
        'city': 'Nairobi',
        'county': 'Nairobi',
        'website': 'https://bluenet.co.ke',
    }
)

if created:
    print(f"✅ Created company: {company.name}")
else:
    print(f"✓ Company already exists: {company.name}")

# Create a tenant for the company
tenant, created = Tenant.objects.get_or_create(
    company=company,
    defaults={
        'subdomain': 'bluenet',
        'database_name': 'bluenet_isp',
        'status': 'trial',
        'trial_days': 30,
        'max_users': 50,
        'max_customers': 1000,
        'monthly_rate': 199.99,
    }
)

if created:
    print(f"✅ Created tenant: {tenant.subdomain}")
    print(f"   Schema name: {tenant.schema_name}")
    print(f"   Status: {tenant.status}")
    print(f"   Trial ends: {tenant.subscription_expiry}")
else:
    print(f"✓ Tenant already exists: {tenant.subdomain}")

# List all tenants
print("\n=== ALL TENANTS ===")
tenants = Tenant.objects.all()
for t in tenants:
    print(f"  - {t.subdomain} ({t.schema_name}) - {t.status}")
    print(f"    Company: {t.company.name}")
    print(f"    Database: {t.database_name}")

print("\n✅ Test tenant setup complete!")
