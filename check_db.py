import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.db import connection

print("=== DATABASE CHECK ===")

# Check core tables
with connection.cursor() as cursor:
    cursor.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public'
        AND table_name LIKE 'core_%'
        ORDER BY table_name
    """)
    tables = cursor.fetchall()
    print(f"Found {len(tables)} core tables:")
    for table in tables:
        print(f"  ✓ {table[0]}")

# Count users
from apps.core.models import User
user_count = User.objects.count()
print(f"\nTotal users in database: {user_count}")

# Check Tenant model
from apps.core.models import Tenant
tenant_count = Tenant.objects.count()
print(f"Total tenants: {tenant_count}")

print("\n✅ Database is properly set up!")
