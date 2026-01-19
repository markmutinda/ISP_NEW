# remove_schema_from_models.py
import os
import re

apps_to_fix = [
    'analytics',
    'bandwidth', 
    'billing',
    'customers',
    'inventory',
    'messaging',
    'network',
    'notifications',
    'self_service',
    'staff',
    'support'
]

for app in apps_to_fix:
    models_path = f'apps/{app}/models.py'
    if os.path.exists(models_path):
        print(f"\nChecking {models_path}...")
        with open(models_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Remove schema_name fields
        original_content = content
        
        # Pattern 1: Full schema_name field with comment
        content = re.sub(r'    # (?:Tenant )?schema field\s*\n    schema_name = models\.\w+\([\s\S]*?\)\s*\n', '\n', content)
        
        # Pattern 2: Simple schema_name field
        content = re.sub(r'\n\s*schema_name = models\.\w+\([^)]*\)\s*\n', '\n', content)
        
        # Remove TenantMixin imports
        content = content.replace('from django_tenants.models import TenantMixin', '')
        content = content.replace('from django_tenants.models import TenantMixin, DomainMixin', '')
        
        # Add app_label to Meta if missing (simple check)
        if 'class Meta:' in content and 'app_label' not in content:
            content = content.replace('class Meta:', 'class Meta:\n        app_label = \'' + app + '\'')
        
        if original_content != content:
            with open(models_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"  ✅ Fixed {app}/models.py")
        else:
            print(f"  ✓ {app}/models.py looks OK")

print("\n✅ All non-core models cleaned up!")
