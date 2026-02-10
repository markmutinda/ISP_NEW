"""Quick test: Create RADIUS user via raw SQL to bypass Django ORM column issues."""
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')
django.setup()

from django.db import connection

# 1. Check radcheck columns
c = connection.cursor()
c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='radcheck' AND table_schema='public' ORDER BY ordinal_position")
cols = [r[0] for r in c.fetchall()]
print(f"radcheck columns: {cols}")

# 2. Check NAS columns
c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='nas' AND table_schema='public' ORDER BY ordinal_position")
nas_cols = [r[0] for r in c.fetchall()]
print(f"nas columns: {nas_cols}")

# 3. Create test user via raw SQL
c.execute("DELETE FROM radcheck WHERE username='test_sim_router'")
c.execute("DELETE FROM radreply WHERE username='test_sim_router'")
c.execute("INSERT INTO radcheck (username, attribute, op, value) VALUES ('test_sim_router', 'Cleartext-Password', ':=', 'test_sim_router')")
c.execute("INSERT INTO radreply (username, attribute, op, value) VALUES ('test_sim_router', 'Mikrotik-Rate-Limit', ':=', '5M/5M')")
c.execute("INSERT INTO radreply (username, attribute, op, value) VALUES ('test_sim_router', 'Session-Timeout', ':=', '3600')")
connection.commit()

# 4. Verify
c.execute("SELECT id, username, attribute, op, value FROM radcheck WHERE username='test_sim_router'")
print("\nradcheck entries:")
for row in c.fetchall():
    print(f"  id={row[0]} user={row[1]} attr={row[2]} op={row[3]} val={row[4]}")

c.execute("SELECT id, username, attribute, op, value FROM radreply WHERE username='test_sim_router'")
print("\nradreply entries:")
for row in c.fetchall():
    print(f"  id={row[0]} user={row[1]} attr={row[2]} op={row[3]} val={row[4]}")

# 5. Check/create default NAS entry
c.execute("SELECT id, nasname, shortname, secret FROM nas")
nas_rows = c.fetchall()
if nas_rows:
    print(f"\nNAS entries: {len(nas_rows)}")
    for row in nas_rows:
        print(f"  id={row[0]} name={row[1]} short={row[2]} secret={row[3][:8]}...")
else:
    print("\nNo NAS entries. Creating default...")
    c.execute("INSERT INTO nas (nasname, shortname, type, secret, description) VALUES ('0.0.0.0/0', 'default', 'other', 'testing123', 'Default NAS')")
    connection.commit()
    print("  Created default NAS (0.0.0.0/0)")

# 6. Check URL patterns
print("\n--- URL Pattern Check ---")
from django.urls import reverse
test_urls = [
    'hotspot-login-page',
    'hotspot-auto-login',
]
for name in test_urls:
    try:
        url = reverse(name)
        print(f"  {name}: {url}")
    except Exception as e:
        print(f"  {name}: {e}")

# Try with app namespace
from django.urls import get_resolver
resolver = get_resolver()
url_names = set()
for pattern in resolver.url_patterns:
    if hasattr(pattern, 'url_patterns'):
        for p in pattern.url_patterns:
            if hasattr(p, 'name') and p.name:
                prefix = pattern.pattern.describe() if hasattr(pattern.pattern, 'describe') else ''
                url_names.add(p.name)

hotspot_urls = [n for n in sorted(url_names) if 'hotspot' in n.lower() or 'cloud' in n.lower() or 'login' in n.lower()]
print(f"\n  Hotspot/Cloud/Login URL names found: {hotspot_urls}")

print("\nDONE - All checks complete")
