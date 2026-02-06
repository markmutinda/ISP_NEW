"""
Script to set up test data for RADIUS expiration testing
Simplified version - directly populates radcheck table for testing
"""
import os
import sys
import django
import dotenv

# Setup Django
dotenv.load_dotenv()
# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()


from django.utils import timezone
from django.db import connection
from datetime import datetime, timedelta


def setup_radius_test_user():
    """
    Create test user directly in radcheck table for FreeRADIUS testing.
    This bypasses Django models and creates data directly in the RADIUS tables.
    """
    from django.db import connection as db_conn
    
    username = 'testpppoe'
    password = 'testpass123'
    
    # Calculate expiration 1 minute from now
    expiration_time = datetime.utcnow() + timedelta(minutes=5)
    # FreeRADIUS expects format: "Feb 06 2026 14:30:00"
    expiration_str = expiration_time.strftime("%b %d %Y %H:%M:%S")
    
    print(f"Setting up RADIUS test user...")
    print(f"Username: {username}")
    print(f"Password: {password}")
    print(f"Expiration: {expiration_str} (1 minute from now)")
    
    with db_conn.cursor() as cursor:
        # First, clean up any existing entries for this test user
        cursor.execute("DELETE FROM radcheck WHERE username = %s", [username])
        cursor.execute("DELETE FROM radreply WHERE username = %s", [username])
        
        # Insert password
        cursor.execute("""
            INSERT INTO radcheck (username, attribute, op, value)
            VALUES (%s, 'Cleartext-Password', ':=', %s)
        """, [username, password])
        print("✓ Password set in radcheck")
        
        # Insert expiration
        cursor.execute("""
            INSERT INTO radcheck (username, attribute, op, value)
            VALUES (%s, 'Expiration', ':=', %s)
        """, [username, expiration_str])
        print("✓ Expiration set in radcheck")
        
        # Insert session timeout (60 seconds) in radreply
        cursor.execute("""
            INSERT INTO radreply (username, attribute, op, value)
            VALUES (%s, 'Session-Timeout', ':=', '60')
        """, [username])
        print("✓ Session-Timeout set in radreply")
        
        # Commit the transaction
        db_conn.commit()
        
        # Verify the entries
        print("\n--- Verifying radcheck entries ---")
        cursor.execute("""
            SELECT id, username, attribute, op, value 
            FROM radcheck 
            WHERE username = %s
        """, [username])
        rows = cursor.fetchall()
        for row in rows:
            print(f"  radcheck: id={row[0]}, user={row[1]}, attr={row[2]}, op={row[3]}, val={row[4]}")
        
        print("\n--- Verifying radreply entries ---")
        cursor.execute("""
            SELECT id, username, attribute, op, value 
            FROM radreply 
            WHERE username = %s
        """, [username])
        rows = cursor.fetchall()
        for row in rows:
            print(f"  radreply: id={row[0]}, user={row[1]}, attr={row[2]}, op={row[3]}, val={row[4]}")
    
    return username, password, expiration_time


def main():
    print("=" * 60)
    print("RADIUS 1-Minute Expiration Test Setup")
    print("=" * 60)
    
    try:
        username, password, expiration = setup_radius_test_user()
        
        print("\n" + "=" * 60)
        print("Test data setup complete!")
        print("=" * 60)
        print(f"""
RADIUS Test Credentials:
  Username: {username}
  Password: {password}
  Expires:  {expiration.strftime('%Y-%m-%d %H:%M:%S')} (1 minute from now)
  
Test Commands (run inside netily_radius container):
  
1. Test authentication NOW (should succeed):
   radtest {username} {password} localhost 0 testing123
   
2. Wait 60+ seconds, then test again (should FAIL with Access-Reject):
   radtest {username} {password} localhost 0 testing123

Expected Results:
- First test: Access-Accept with Session-Timeout=60
- Second test (after expiration): Access-Reject

To extend the subscription (reset expiration to 1 more minute):
  python setup_test_data.py
""")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
