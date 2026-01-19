# quick_test.py
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

try:
    import django
    django.setup()
    
    print("✅ Django setup successful")
    
    # Try to import models
    from apps.core.models import Tenant, User
    print("✅ Imported core models")
    
    from apps.customers.models import Customer
    print("✅ Imported Customer model")
    
    # Check inheritance
    from django_tenants.models import TenantMixin
    
    if issubclass(Tenant, TenantMixin):
        print("✅ Tenant inherits from TenantMixin (CORRECT - this is what we want!)")
    else:
        print("❌ Tenant does NOT inherit from TenantMixin (WRONG - it should!)")
    
    if issubclass(Customer, TenantMixin):
        print("❌ Customer inherits from TenantMixin (WRONG - it should NOT!)")
    else:
        print("✅ Customer does NOT inherit from TenantMixin (CORRECT!)")
        
    print("\n✅ All checks passed!")
    
except Exception as e:
    print(f"❌ Error: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
