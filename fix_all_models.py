# fix_all_models.py
import os
import re

# Update AuditMixin in core/models.py
core_models_path = 'apps/core/models.py'
if os.path.exists(core_models_path):
    with open(core_models_path, 'r') as f:
        content = f.read()
    
    # Check if AuditMixin uses 'User' instead of settings.AUTH_USER_MODEL
    if "'User'" in content or '"User"' in content:
        # Replace with settings.AUTH_USER_MODEL
        content = re.sub(r"models\.ForeignKey\s*\(\s*['\"]User['\"]", 
                        "models.ForeignKey(settings.AUTH_USER_MODEL", content)
        
        # Make sure settings is imported
        if 'from django.conf import settings' not in content:
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if 'from django.db import models' in line:
                    lines.insert(i+1, 'from django.conf import settings')
                    break
            content = '\n'.join(lines)
        
        with open(core_models_path, 'w') as f:
            f.write(content)
        print("✅ Updated AuditMixin in core/models.py")

# Fix network models
network_model_files = [
    'apps/network/models/olt_models.py',
    'apps/network/models/tr069_models.py',
    'apps/network/models/mikrotik_models.py',
    'apps/network/models/ipam_models.py',
]

for file_path in network_model_files:
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            content = f.read()
        
        changes_made = False
        
        # Fix applied_by and initiated_by fields
        if 'applied_by = models.ForeignKey' in content:
            content = re.sub(
                r"applied_by = models\.ForeignKey\s*\(\s*['\"]auth\.User['\"]",
                "applied_by = models.ForeignKey(settings.AUTH_USER_MODEL",
                content
            )
            changes_made = True
        
        if 'initiated_by = models.ForeignKey' in content:
            content = re.sub(
                r"initiated_by = models\.ForeignKey\s*\(\s*['\"]auth\.User['\"]",
                "initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL",
                content
            )
            changes_made = True
        
        # Fix PPPoEUser local_address and remote_address fields
        if file_path.endswith('mikrotik_models.py'):
            content = re.sub(
                r"local_address = models\.GenericIPAddressField\(protocol='IPv4', blank=True\)",
                "local_address = models.GenericIPAddressField(protocol='IPv4', null=True, blank=True)",
                content
            )
            content = re.sub(
                r"remote_address = models\.GenericIPAddressField\(protocol='IPv4', blank=True\)",
                "remote_address = models.GenericIPAddressField(protocol='IPv4', null=True, blank=True)",
                content
            )
            changes_made = True
        
        # Add settings import if needed
        if changes_made and 'from django.conf import settings' not in content:
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if 'from django.db import models' in line:
                    lines.insert(i+1, 'from django.conf import settings')
                    break
            content = '\n'.join(lines)
        
        if changes_made:
            with open(file_path, 'w') as f:
                f.write(content)
            print(f"✅ Fixed {file_path}")

print("\n✅ All fixes applied!")
print("\nNow run these commands:")
print("1. Clear cache: find . -name \"*.pyc\" -delete")
print("2. Run check: python manage.py check")
print("3. Make migrations: python manage.py makemigrations network")
print("4. Apply migrations: python manage.py migrate network")