# fix_customers_models.py
import re

filepath = "apps/customers/models.py"
print(f"Fixing {filepath}...")

with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Remove schema_name fields from all models
# Pattern to find and remove schema_name field definitions
schema_pattern = r'    # Tenant schema field\s*\n    schema_name = models\.SlugField\([\s\S]*?default="default_schema"\)\s*\n'
content = re.sub(schema_pattern, '', content)

# Also remove any leftover schema_name fields that might be in different format
content = re.sub(r'\n\s*schema_name = models\.\w+\([\s\S]*?default="default_schema"[^)]*\)\s*\n', '\n', content)
content = re.sub(r'\n\s*schema_name = models\.\w+\([\s\S]*?default=\'default_schema\'[^)]*\)\s*\n', '\n', content)

# 2. Remove TenantMixin comments from class definitions
lines = content.split('\n')
fixed_lines = []
for line in lines:
    # Remove "← Changed to TenantMixin" comments
    line = re.sub(r'# ← Changed to TenantMixin', '', line)
    line = re.sub(r'# ← Changed', '', line)
    fixed_lines.append(line)

content = '\n'.join(fixed_lines)

# 3. Remove TenantMixin import if it exists
if 'from django_tenants.models import TenantMixin' in content:
    content = content.replace('from django_tenants.models import TenantMixin', '')

# 4. Make sure models are inheriting from models.Model
# Already looks correct in your file

# 5. Add app_label to Meta classes if missing
# Check each model's Meta class
models_to_check = [
    'Customer',
    'CustomerAddress', 
    'CustomerDocument',
    'NextOfKin',
    'CustomerNotes',
    'ServiceConnection'
]

for model in models_to_check:
    # Pattern to find class definition
    class_pattern = rf'class {model}\([^)]*\):'
    if re.search(class_pattern, content):
        # Find the Meta class for this model
        meta_pattern = rf'(class {model}\([^)]*\):[\s\S]*?class Meta:)'
        match = re.search(meta_pattern, content)
        if match:
            meta_block = match.group(0)
            # Check if app_label is already in Meta
            if 'app_label' not in meta_block:
                # Add app_label after class Meta:
                new_meta_block = meta_block.replace('class Meta:', 'class Meta:\n        app_label = \'customers\'')
                content = content.replace(meta_block, new_meta_block)

# 6. Write the fixed content back
with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)

print("✅ Fixed customers/models.py")
print("Removed:")
print("  - schema_name fields from all models")
print("  - TenantMixin comments")
print("  - Added app_label to Meta classes")

# Let's verify the fix
print("\n--- Verification ---")
with open(filepath, 'r', encoding='utf-8') as f:
    fixed_content = f.read()
    
# Check for remaining schema_name
if 'schema_name = models' in fixed_content:
    print("❌ WARNING: schema_name field still exists!")
else:
    print("✅ No schema_name fields found")

# Check for TenantMixin in class definitions
if re.search(r'class \w+\([^)]*TenantMixin[^)]*\):', fixed_content):
    print("❌ WARNING: TenantMixin found in class definitions!")
else:
    print("✅ No TenantMixin in class definitions")

print("\nFirst few lines to verify:")
for i, line in enumerate(fixed_content.split('\n')[:20]):
    print(f"{i+1}: {line}")
