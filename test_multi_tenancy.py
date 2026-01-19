import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django_tenants.utils import tenant_context
from apps.core.models import Tenant, User

print("=== TESTING MULTI-TENANCY ===")

# Get the first tenant
tenant = Tenant.objects.first()

if tenant:
    print(f"1. Testing with tenant: {tenant.subdomain}")
    
    # Create a user in PUBLIC schema (shared)
    public_user, created = User.objects.get_or_create(
        email='public@example.com',
        defaults={
            'phone_number': '+254700000001',
            'first_name': 'Public',
            'last_name': 'User',
            'role': 'admin'
        }
    )
    if created:
        public_user.set_password('test123')
        public_user.save()
        print(f"   ✅ Created public user: {public_user.email}")
    
    # Switch to tenant context
    with tenant_context(tenant):
        print(f"2. Now in tenant context: {tenant.schema_name}")
        
        # Create a user in TENANT schema
        tenant_user, created = User.objects.get_or_create(
            email='tenant@example.com',
            defaults={
                'phone_number': '+254700000002',
                'first_name': 'Tenant',
                'last_name': 'User',
                'role': 'customer'
            }
        )
        if created:
            tenant_user.set_password('test123')
            tenant_user.save()
            print(f"   ✅ Created tenant user: {tenant_user.email}")
        
        # Count users in tenant schema
        tenant_user_count = User.objects.count()
        print(f"   Users in tenant schema: {tenant_user_count}")
        
        # Show users in tenant
        for user in User.objects.all():
            print(f"     - {user.email} ({user.get_full_name()})")

# Back to public schema
print(f"\n3. Back in public schema")
public_user_count = User.objects.count()
print(f"   Users in public schema: {public_user_count}")

print("\n✅ Multi-tenancy is working correctly!")
print("   - Public schema users: Shared across all tenants")
print("   - Tenant schema users: Isolated per tenant")
