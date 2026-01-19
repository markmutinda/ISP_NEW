# verify_fix.py
import os
import re

print("=== FINAL VERIFICATION ===\n")

# Check 1: Only core/models.py should have TenantMixin inheritance
print("1. Checking TenantMixin inheritance...")
tenant_mixin_files = []
for root, dirs, files in os.walk('apps'):
    for file in files:
        if file == 'models.py':
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                # Check for class inheritance of TenantMixin
                if re.search(r'class \w+\([^)]*TenantMixin[^)]*\):', content):
                    tenant_mixin_files.append(filepath)

print(f"Files with TenantMixin inheritance: {len(tenant_mixin_files)}")
for f in tenant_mixin_files:
    if 'core/models.py' in f:
        print(f"  ✅ {f} (this should have TenantMixin)")
    else:
        print(f"  ❌ {f} (should NOT have TenantMixin)")

# Check 2: No schema_name fields in non-core models
print("\n2. Checking schema_name fields in non-core models...")
schema_name_files = []
for root, dirs, files in os.walk('apps'):
    for file in files:
        if file == 'models.py' and 'core' not in root:
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                if 'schema_name = models.' in content:
                    schema_name_files.append(filepath)

print(f"Non-core files with schema_name: {len(schema_name_files)}")
for f in schema_name_files:
    print(f"  ❌ {f}")

# Check 3: All models have app_label
print("\n3. Checking app_label in Meta classes...")
missing_app_label = []
for root, dirs, files in os.walk('apps'):
    for file in files:
        if file == 'models.py':
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                # Find all model classes
                model_classes = re.findall(r'class (\w+)\([^)]*\):', content)
                for model in model_classes:
                    # Look for Meta class for this model
                    # Simple pattern: from class to next class or end
                    pattern = rf'class {model}\([^)]*\):(.*?)(?=class \w+\(|$)'
                    match = re.search(pattern, content, re.DOTALL)
                    if match:
                        model_content = match.group(1)
                        if 'class Meta:' in model_content and 'app_label' not in model_content:
                            app_name = root.split('\\')[-1]  # Get app name from path
                            missing_app_label.append(f"{app_name}.{model}")

if missing_app_label:
    print(f"Models missing app_label: {len(missing_app_label)}")
    for m in missing_app_label:
        print(f"  ⚠️  {m}")
else:
    print("✅ All models have app_label")

print("\n=== SUMMARY ===")
if len(tenant_mixin_files) == 1 and 'core/models.py' in tenant_mixin_files[0]:
    print("✅ PASS: Only core has TenantMixin")
else:
    print("❌ FAIL: Multiple files have TenantMixin")

if len(schema_name_files) == 0:
    print("✅ PASS: No schema_name in non-core models")
else:
    print("❌ FAIL: schema_name found in non-core models")

print("\n✅ Verification complete!")
