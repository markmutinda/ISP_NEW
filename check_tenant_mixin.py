import os
import re

print("Checking for TenantMixin in files...")
found = []
for root, dirs, files in os.walk('.'):
    for file in files:
        if file.endswith('.py'):
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                if 'TenantMixin' in content:
                    # Check if it's in a class definition
                    if 'class ' in content and 'TenantMixin' in content:
                        found.append(filepath)

print("\nFiles with TenantMixin in class definitions:")
for f in found:
    print(f"  - {f}")

if len(found) > 1:
    print(f"\n❌ ERROR: Found {len(found)} files with TenantMixin!")
    print("Only core/models.py should have TenantMixin.")
else:
    print("\n✅ OK: Only core/models.py has TenantMixin (or none found).")
