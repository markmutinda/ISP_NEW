# test_network_simple.py
import os
import django
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')

try:
    django.setup()
    print("✅ Django setup successful")
    
    # Test imports
    from apps.core.models import Company
    print("✅ Imported Company model")
    
    from apps.network.models.olt_models import OLTDevice
    print("✅ Imported OLTDevice model")
    
    from apps.network.models.ipam_models import Subnet
    print("✅ Imported Subnet model")
    
    # Check if we can query
    company_count = Company.objects.count()
    print(f"✅ Company count: {company_count}")
    
    print("\n🎉 All network imports successful!")
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
    