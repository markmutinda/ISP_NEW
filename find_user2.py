import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')
django.setup()

from django_tenants.utils import schema_context
from django.contrib.auth import get_user_model
User = get_user_model()

try:
    from apps.core.models import Tenant
    tenants = list(Tenant.objects.all())
    print(f"Found {len(tenants)} tenants:")
    for t in tenants:
        print(f"  - {t.schema_name} / {t.name}")
except Exception as e:
    print(f"Error: {e}")

# Search in public schema too
print("\n--- Searching public schema ---")
try:
    users = User.objects.all()[:20]
    for u in users:
        print(f"  Email: {u.email}, Phone: {u.phone_number}, Active: {u.is_active}")
except Exception as e:
    print(f"Error: {e}")

# Search each tenant
for t in tenants:
    print(f"\n--- Tenant: {t.schema_name} ---")
    try:
        with schema_context(t.schema_name):
            count = User.objects.count()
            print(f"  Total users: {count}")
            users = User.objects.all()[:10]
            for u in users:
                print(f"  Email: {u.email}, Phone: {u.phone_number}")
    except Exception as e:
        print(f"  Error: {e}")
