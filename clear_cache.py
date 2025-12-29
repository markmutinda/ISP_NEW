import os
import shutil
import sys

print("Clearing all Python caches...")

folders = [
    '__pycache__',
    'config/__pycache__',
    'config/settings/__pycache__',
    'apps/__pycache__',
    'apps/core/__pycache__',
    'apps/customers/__pycache__',
]

for folder in folders:
    if os.path.exists(folder):
        shutil.rmtree(folder)
        print(f"Deleted: {folder}")

print("\nCache cleared! Now close this terminal and open a new one.")
print("Then run:")
print("python manage.py makemigrations customers")
print("python manage.py migrate")