"""Check tenant schemas and migrate"""
import os, sys, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')
sys.path.insert(0, '/app')
django.setup()

from django.db import connection

cursor = connection.cursor()
cursor.execute("SELECT schema_name FROM information_schema.schemata ORDER BY schema_name")
print("=== Database Schemas ===")
for row in cursor.fetchall():
    print("  " + row[0])

# Check if we have tenants
print()
print("=== Tenants ===")
try:
    from django_tenants.utils import get_tenant_model
    TenantModel = get_tenant_model()
    for t in TenantModel.objects.all():
        print("  {} (schema: {})".format(t, t.schema_name))
except Exception as e:
    print("  Error: {}".format(e))

# Check current schema
cursor.execute("SELECT current_schema()")
print()
print("Current schema: {}".format(cursor.fetchone()[0]))

# Check if network_router exists in any schema
cursor.execute("""
    SELECT table_schema, table_name 
    FROM information_schema.tables 
    WHERE table_name = 'network_router'
    ORDER BY table_schema
""")
rows = cursor.fetchall()
print()
print("=== network_router table locations ===")
if rows:
    for row in rows:
        print("  schema: {}, table: {}".format(row[0], row[1]))
else:
    print("  NOT FOUND in any schema")
