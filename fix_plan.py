import os
import sys
import django
import dotenv

# 1. SETUP DJANGO
dotenv.load_dotenv()
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

# 2. IMPORT MODULES
from django_tenants.utils import schema_context
from apps.billing.models import Plan

tenant = 'tenant_yellow1'
print(f'--- 🔧 UPDATING PLAN IN {tenant} ---')

try:
    with schema_context(tenant):
        # Get the plan by ID (ID 3 is the one we've been using)
        p = Plan.objects.get(id=3)
        
        # Update Name
        p.name = "Test-15min"
        
        # Update Validity
        p.validity_type = 'MINUTES'
        p.validity_minutes = 15
        
        # 🚨 Use real field name for days
        p.duration_days = 0 
        
        p.save()
        
        print(f'✅ SUCCESS: Plan updated to "{p.name}" (ID: {p.id})')
        print(f'   Validity: {p.validity_minutes} MINUTES')

except Exception as e:
    print(f'❌ Error: {e}')