# fix_app_labels.py
import os
import re

print("=== Fixing app_label for all models ===\n")

for root, dirs, files in os.walk('apps'):
    for file in files:
        if file == 'models.py':
            filepath = os.path.join(root, file)
            app_name = os.path.basename(root)  # Get app name from folder
            
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            print(f"Processing {app_name}/models.py...")
            
            # Find all model classes
            model_pattern = r'(class (\w+)\([^)]*\):[\s\S]*?)(?=\nclass |\Z)'
            models = re.findall(model_pattern, content, re.MULTILINE)
            
            updated_content = content
            changes_made = []
            
            for model_block, model_name in models:
                # Check if this model has a Meta class
                if 'class Meta:' in model_block:
                    # Check if app_label is already present
                    if 'app_label' not in model_block:
                        # Add app_label to Meta class
                        new_model_block = model_block.replace(
                            'class Meta:',
                            f'class Meta:\n        app_label = \'{app_name}\''
                        )
                        updated_content = updated_content.replace(model_block, new_model_block)
                        changes_made.append(model_name)
                else:
                    # Model doesn't have a Meta class, we should add one
                    # But only if it's not an abstract model (like BaseModel, AuditMixin)
                    if not any(keyword in model_block for keyword in ['class Meta:', 'abstract = True']):
                        # Add a simple Meta class with app_label
                        if model_name not in ['BaseModel', 'AuditMixin', 'UserManager']:
                            new_model_block = model_block.rstrip() + '\n\n    class Meta:\n        app_label = \'' + app_name + '\''
                            updated_content = updated_content.replace(model_block, new_model_block)
                            changes_made.append(model_name)
            
            # Write back if changes were made
            if changes_made:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(updated_content)
                print(f"  ✅ Added app_label to: {', '.join(changes_made)}")
            else:
                print("  ✓ No changes needed")

print("\n✅ All app_labels fixed!")
