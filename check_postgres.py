import os
import sys
import psycopg2
from psycopg2 import OperationalError

print("=== POSTGRESQL CONNECTION CHECK ===")

# Try to connect without password (trust method)
try:
    conn = psycopg2.connect(
        host="localhost",
        port="5432",
        dbname="isp_management",
        DB_PASSWORD=2202 
        user="isp_user",
        connect_timeout=3
    )
    print("✅ PostgreSQL is running")
    conn.close()
except OperationalError as e:
    print(f"❌ Connection error: {e}")
    print("\nTroubleshooting steps:")
    print("1. Make sure PostgreSQL is running:")
    print("   - Windows: Check Services for 'postgresql'")
    print("   - Run: net start postgresql")
    print("\n2. Check your pg_hba.conf file:")
    print("   - Location: C:\\Program Files\\PostgreSQL\\<version>\\data\\pg_hba.conf")
    print("   - For development, you can set:")
    print("     host    all    all    127.0.0.1/32    trust")
    print("\n3. Or use password authentication in Django settings:")
    print("   - Add 'PASSWORD': 'yourpassword' to DATABASES config")