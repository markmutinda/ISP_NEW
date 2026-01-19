import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

print("=== Resetting migration history ===")

# Delete all entries from django_migrations table
with connection.cursor() as cursor:
    cursor.execute("DELETE FROM django_migrations WHERE app IN ('core', 'customers', 'inventory', 'staff', 'support', 'notifications', 'messaging', 'self_service', 'analytics', 'bandwidth')")
    print("Deleted custom app migration records")
    
    # Also delete default app migrations that depend on core
    cursor.execute("DELETE FROM django_migrations WHERE app IN ('admin', 'auth', 'contenttypes', 'sessions')")
    print("Deleted default app migration records")

print("Migration history reset complete!")
print("\nNow run these commands:")
print("1. python manage.py migrate_schemas --shared")
print("2. python manage.py migrate_schemas")
