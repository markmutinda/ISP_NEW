# fix_tenant_mixin.py
import os
import re

def fix_models():
    # Files to fix (all except core/models.py)
    files_to_fix = [
        'apps/analytics/models.py',
        'apps/bandwidth/models.py',
        'apps/billing/models/billing_models.py',
        'apps/billing/models/payment_models.py',
        'apps/billing/models/voucher_models.py',
        'apps/customers/models.py',
        'apps/inventory/models.py',
        'apps/messaging/models.py',
        'apps/network/models/ipam_models.py',
        'apps/network/models/olt_models.py',
        'apps/network/models/router_models.py',
        'apps/network/models/tr069_models.py',
        'apps/notifications/models.py',
        'apps/self_service/models.py',
        'apps/staff/models.py',
        'apps/support/models.py',
    ]
    
    for filepath in files_to_fix:
        if os.path.exists(filepath):
            print(f"Fixing {filepath}...")
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Remove TenantMixin from class definitions
            # Pattern 1: class ModelName(TenantMixin, ...):
            content = re.sub(r'class (\w+)\(TenantMixin,\s*', r'class \1(', content)
            # Pattern 2: class ModelName(..., TenantMixin):
            content = re.sub(r',\s*TenantMixin\):', r'):', content)
            # Pattern 3: class ModelName(TenantMixin):
            content = re.sub(r'class (\w+)\(TenantMixin\):', r'class \1(models.Model):', content)
            
            # Add necessary imports if needed
            if 'from django_tenants.models import TenantMixin' in content:
                # Remove the import if no longer needed
                content = content.replace('from django_tenants.models import TenantMixin', '')
            
            # Write back
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"✓ Fixed {filepath}")

    print("\n✅ All models fixed! Only core.Tenant should inherit from TenantMixin.")

if __name__ == '__main__':
    fix_models()