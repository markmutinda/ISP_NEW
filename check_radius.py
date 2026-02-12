import os
import django

# 1. Setup Django Environment
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from django.db import connection

print('\n=== RADIUS DATABASE CHECK ===')
cursor = connection.cursor()

try:
    # 2. Check Tables
    cursor.execute('SELECT * FROM radcheck LIMIT 1'); 
    print('✅ PASS: radcheck table is accessible')
    
    cursor.execute('SELECT * FROM nas LIMIT 1'); 
    print('✅ PASS: nas table is accessible')

    # 3. Create Test User (Raw SQL)
    print('--- Creating Test User ---')
    cursor.execute("INSERT INTO radcheck (username, attribute, op, value) VALUES ('test_sim_router', 'Cleartext-Password', ':=', 'test_pass') ON CONFLICT DO NOTHING")
    cursor.execute("INSERT INTO radreply (username, attribute, op, value) VALUES ('test_sim_router', 'Mikrotik-Rate-Limit', ':=', '5M/5M') ON CONFLICT DO NOTHING")
    cursor.execute("INSERT INTO nas (nasname, shortname, type, secret, description) VALUES ('0.0.0.0/0', 'default', 'other', 'testing123', 'Default NAS') ON CONFLICT (nasname) DO NOTHING")
    
    connection.commit()
    print('✅ PASS: User and Default NAS created successfully')

except Exception as e:
    print(f'❌ FAIL: Database Error: {e}')