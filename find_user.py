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
except Exception as e:
    print(f"Error loading tenants: {e}")
    tenants = []

for t in tenants:
    try:
        with schema_context(t.schema_name):
            users = User.objects.filter(phone_number__contains='721591249')
            for u in users:
                print(f"Tenant: {t.schema_name}")
                print(f"  Email: {u.email}")
                print(f"  Phone: {u.phone_number}")
                print(f"  Active: {u.is_active}")
                print(f"  Password hash: {u.password[:30]}...")
                try:
                    from apps.customers.models import Customer
                    c = Customer.objects.get(user=u)
                    print(f"  Customer name: {c.full_name}")
                    print(f"  PPPoE username: {c.pppoe_username}")
                except Exception as ce:
                    print(f"  No customer record: {ce}")
    except Exception as e:
        print(f"Error in tenant {t.schema_name}: {e}")

print("Done.")
