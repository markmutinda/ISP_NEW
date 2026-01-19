import os
import sys

# Set the correct settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')

# Add project root to path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

try:
    import django
    django.setup()
    
    print("=== FIXING DJANGO-TENANTS SETUP ===")
    
    # 1. Check settings
    from django.conf import settings
    print(f"1. Using settings: {settings.SETTINGS_MODULE}")
    print(f"   TENANT_MODEL: {getattr(settings, 'TENANT_MODEL', 'NOT SET!')}")
    print(f"   INSTALLED_APPS has django_tenants: {'django_tenants' in settings.INSTALLED_APPS}")
    
    # 2. Create migrations
    from django.core.management import call_command
    
    print("\n2. Creating migrations...")
    try:
        call_command('makemigrations', 'core')
        print("   ✅ Created core migrations")
    except Exception as e:
        print(f"   ❌ Error creating core migrations: {e}")
    
    # 3. Try to run regular migrate first (not migrate_schemas)
    print("\n3. Running initial migrations...")
    try:
        call_command('migrate', 'contenttypes', verbosity=0)
        call_command('migrate', 'auth', verbosity=0)
        print("   ✅ Basic Django migrations applied")
    except Exception as e:
        print(f"   ❌ Error in basic migrations: {e}")
    
    # 4. Check if core migrations exist
    core_migrations = os.path.join(project_root, 'apps', 'core', 'migrations')
    if os.path.exists(core_migrations):
        print(f"\n4. Core migrations found at: {core_migrations}")
        files = os.listdir(core_migrations)
        py_files = [f for f in files if f.endswith('.py') and f != '__init__.py']
        print(f"   Migration files: {py_files}")
    
    print("\n=== NEXT STEPS ===")
    print("1. Run: python manage.py makemigrations")
    print("2. Run: python manage.py migrate")
    print("3. Run: python manage.py createsuperuser")
    print("\nIf that works, then run:")
    print("4. python manage.py migrate_schemas --shared")
    
except Exception as e:
    print(f"=== FATAL ERROR ===")
    print(f"Error: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
