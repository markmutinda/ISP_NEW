#!/usr/bin/env python
"""Test PostgreSQL connection"""
import os
import sys
import psycopg2
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Get database credentials
db_config = {
    'dbname': os.getenv('DB_NAME', 'isp_management'),
    'user': os.getenv('DB_USER', 'isp_user'),
    'password': os.getenv('DB_PASSWORD', 'isp_password123'),
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': os.getenv('DB_PORT', '5432'),
}

print("Testing PostgreSQL connection...")
print(f"Database: {db_config['dbname']}")
print(f"User: {db_config['user']}")
print(f"Host: {db_config['host']}:{db_config['port']}")

try:
    conn = psycopg2.connect(**db_config)
    cursor = conn.cursor()
    
    # Test query
    cursor.execute("SELECT version();")
    version = cursor.fetchone()
    
    print("\n✅ Connection successful!")
    print(f"PostgreSQL version: {version[0]}")
    
    # List databases
    cursor.execute("SELECT datname FROM pg_database WHERE datistemplate = false;")
    databases = cursor.fetchall()
    print("\nAvailable databases:")
    for db in databases:
        print(f"  - {db[0]}")
    
    cursor.close()
    conn.close()
    
except psycopg2.OperationalError as e:
    print(f"\n❌ Connection failed: {e}")
    print("\nTroubleshooting steps:")
    print("1. Check if PostgreSQL service is running")
    print("2. Verify credentials in .env file")
    print("3. Check if database/user exists")
    print("4. Try connecting with pgAdmin")
    
except Exception as e:
    print(f"\n❌ Error: {e}")