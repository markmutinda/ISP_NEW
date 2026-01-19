import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.db import connection

print("=== RESETTING MIGRATION HISTORY ===")

with connection.cursor() as cursor:
    # Drop all tables (DANGEROUS - only for development!)
    cursor.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public'
    """)
    tables = cursor.fetchall()
    
    if tables:
        print(f"Dropping {len(tables)} tables...")
        for table in tables:
            cursor.execute(f'DROP TABLE IF EXISTS "{table[0]}" CASCADE')
        print("All tables dropped!")
    
    # Clear django_migrations table
    cursor.execute("DROP TABLE IF EXISTS django_migrations")
    print("Migration history cleared!")

print("\n✅ Database reset complete!")
print("\nNow run these commands:")
print("1. python manage.py makemigrations")
print("2. python manage.py migrate")
print("3. python manage.py createsuperuser")
