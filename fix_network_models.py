# fix_network_models.py
import os
import re

# List of all network model files
network_model_files = [
    'apps/network/models/olt_models.py',
    'apps/network/models/tr069_models.py',
    'apps/network/models/mikrotik_models.py',
    'apps/network/models/ipam_models.py',
]

for file_path in network_model_files:
    if os.path.exists(file_path):
        print(f"Fixing: {file_path}")
        with open(file_path, 'r') as f:
            content = f.read()
        
        # Fix 1: Replace BaseModel with AuditMixin and add models import
        if 'from apps.core.models import Company, BaseModel' in content:
            content = content.replace(
                'from apps.core.models import Company, BaseModel',
                'from django.db import models\nfrom apps.core.models import Company, AuditMixin'
            )
        
        # Fix 2: Replace BaseModel with AuditMixin in class definitions
        content = re.sub(r'class (\w+)\(BaseModel\):', r'class \1(AuditMixin, models.Model):', content)
        
        # Fix 3: Fix MaxValueValidator imports
        if 'models.MaxValueValidator' in content:
            content = re.sub(
                r'validators=\[models\.(MaxValueValidator|MinValueValidator)',
                r'validators=[\1',
                content
            )
        
        # Fix 4: Add proper imports for validators
        if any(validator in content for validator in ['MaxValueValidator', 'MinValueValidator', 'validate_ipv4_address']):
            if 'from django.core.validators import' not in content:
                # Add import at the top after django.db import
                lines = content.split('\n')
                for i, line in enumerate(lines):
                    if line.startswith('from django.db import models'):
                        # Check what validators are needed
                        validators_needed = []
                        if 'MaxValueValidator' in content:
                            validators_needed.append('MaxValueValidator')
                        if 'MinValueValidator' in content:
                            validators_needed.append('MinValueValidator')
                        if 'validate_ipv4_address' in content:
                            validators_needed.append('validate_ipv4_address')
                        
                        if validators_needed:
                            lines.insert(i + 1, f'from django.core.validators import {", ".join(validators_needed)}')
                        break
                content = '\n'.join(lines)
        
        with open(file_path, 'w') as f:
            f.write(content)
        
        print(f"  ✓ Fixed: {file_path}")
    else:
        print(f"  ✗ File not found: {file_path}")

print("\n✅ All network model files have been fixed!")