import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

print("=== Testing Django Setup ===")

# Test imports
from django.apps import apps
from django.db import connection

print("1. Checking installed apps...")
for app in apps.get_app_configs():
    print(f"   - {app.label}: {app.name}")

print("\n2. Checking core models...")
try:
    from apps.core.models import Tenant, User, Company
    print("   ✓ Core models imported successfully")
    
    # Check if Tenant inherits from TenantMixin
    from django_tenants.models import TenantMixin
    if issubclass(Tenant, TenantMixin):
        print("   ✓ Tenant model correctly inherits from TenantMixin")
    else:
        print("   ✗ Tenant model does NOT inherit from TenantMixin")
        
except Exception as e:
    print(f"   ✗ Error importing core models: {e}")

print("\n3. Checking database tables...")
with connection.cursor() as cursor:
    cursor.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public' 
        AND table_name LIKE 'core_%'
    """)
    tables = cursor.fetchall()
    print(f"   Found {len(tables)} core tables:")
    for table in tables:
        print(f"     - {table[0]}")

print("\n=== Test Complete ===")
