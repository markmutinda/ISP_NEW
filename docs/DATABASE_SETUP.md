# Netily ISP Management - Database Setup Guide

This guide explains how to set up the database from scratch for the Netily ISP Management platform.

## Prerequisites

- PostgreSQL 14+ installed and running
- Python 3.11+ with virtual environment activated
- All dependencies installed (`pip install -r requirements/local.txt`)

## Overview

Netily uses **django-tenants** for multi-tenancy with PostgreSQL schemas:

| Schema | Purpose |
|--------|---------|
| `public` | Shared data: Companies, Tenants, Domains, Subscription Plans |
| `<tenant>` | Tenant-specific data: Users, Customers, Invoices, etc. |

---

## Step 1: Create the PostgreSQL Database

```bash
# Connect to PostgreSQL
psql -U postgres

# Create the database
CREATE DATABASE isp_management;

# Exit
\q
```

Or using command line:
```bash
createdb -U postgres isp_management
```

---

## Step 2: Configure Environment Variables

Create a `.env` file in the project root:

```env
# Database
DB_NAME=isp_management
DB_USER=postgres
DB_PASSWORD=your_password
DB_HOST=localhost
DB_PORT=5432

# Django
SECRET_KEY=your-secret-key-here
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
```

---

## Step 3: Run Shared Schema Migrations

This creates all tables in the `public` schema:

```bash
python manage.py migrate_schemas --shared
```

Expected output:
```
[standard:public] === Starting migration
[standard:public] Operations to perform:
[standard:public]   Apply all migrations: admin, auth, core, subscriptions...
[standard:public] Running migrations:
[standard:public]   Applying contenttypes.0001_initial... OK
[standard:public]   Applying auth.0001_initial... OK
[standard:public]   Applying core.0001_initial... OK
...
```

---

## Step 4: Create the Public Tenant

The public tenant is required for django-tenants to work. Run this script:

```python
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.local'
import django
django.setup()

from django.utils.text import slugify
from apps.core.models import Company, Tenant, Domain

# Create the Netily platform company
netily_company, c_created = Company.objects.get_or_create(
    name='Netily Platform',
    defaults={
        'slug': 'netily-platform',
        'company_type': 'isp',
        'email': 'admin@netily.io',
        'phone_number': '+254700000000',
        'address': 'Nairobi, Kenya',
        'city': 'Nairobi',
        'county': 'Nairobi',
        'is_active': True
    }
)
print(f'Netily company created: {c_created}')

# Create the public tenant
public_tenant, t_created = Tenant.objects.get_or_create(
    schema_name='public',
    defaults={
        'company': netily_company,
        'subdomain': 'public',
        'database_name': 'public',
        'status': 'active',
        'is_active': True
    }
)
print(f'Public tenant created: {t_created}')

# Create domains for public tenant
Domain.objects.get_or_create(
    domain='localhost',
    defaults={'tenant': public_tenant, 'is_primary': True}
)
Domain.objects.get_or_create(
    domain='127.0.0.1',
    defaults={'tenant': public_tenant, 'is_primary': False}
)
print('Public tenant domains created')
```

Save as `setup_public_tenant.py` and run:
```bash
python setup_public_tenant.py
```

---

## Step 5: Load Subscription Plans

Load the Netily subscription plans fixture:

```bash
python manage.py loaddata apps/subscriptions/fixtures/netily_plans.json
```

This creates 4 plans:
- **Test Plan** - KES 1/month (for testing)
- **Starter** - KES 2,999/month (100 subscribers, 5 routers, 3 staff)
- **Professional** - KES 7,999/month (500 subscribers, 25 routers, 10 staff)
- **Enterprise** - KES 19,999/month (Unlimited)

---

## Step 6: Create Superuser Account

```python
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.local'
import django
django.setup()

from apps.core.models import User, Company

# Get the Netily company
netily_company = Company.objects.get(name='Netily Platform')

# Create superuser
user, created = User.objects.get_or_create(
    email='admin@netily.io',
    defaults={
        'first_name': 'Admin',
        'last_name': 'User',
        'is_staff': True,
        'is_superuser': True,
        'is_active': True,
        'is_verified': True,
        'role': 'admin',
        'company': netily_company,
    }
)
user.set_password('Admin@2026')
user.save()
print(f'Superuser created: {created}')
print('Email: admin@netily.io')
print('Password: Admin@2026')
```

---

## Step 7: Create a Sample ISP Tenant

### 7.1 Create the Company and Tenant

```python
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.local'
import django
django.setup()

from apps.core.models import Company, Tenant, Domain

# Create ISP company
sample_company, c_created = Company.objects.get_or_create(
    name='Yellow ISP',
    defaults={
        'slug': 'yellow-isp',
        'company_type': 'isp',
        'email': 'admin@yellowisp.com',
        'phone_number': '+254712345678',
        'address': 'Mombasa Road',
        'city': 'Nairobi',
        'county': 'Nairobi',
        'is_active': True
    }
)
print(f'Company created: {c_created}')

# Create tenant (this auto-creates the schema and runs migrations)
sample_tenant, t_created = Tenant.objects.get_or_create(
    subdomain='yellow',
    defaults={
        'schema_name': 'yellow',
        'company': sample_company,
        'database_name': 'yellow',
        'status': 'active',
        'is_active': True
    }
)
print(f'Tenant created: {t_created}')

# Create domain
Domain.objects.get_or_create(
    domain='yellow.localhost',
    defaults={'tenant': sample_tenant, 'is_primary': True}
)
print('Domain created: yellow.localhost')
```

### 7.2 Create Tenant Admin User

```python
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.local'
import django
django.setup()

from django.db import connection
from apps.core.models import Tenant, User

# Get the tenant
tenant = Tenant.objects.get(subdomain='yellow')

# Switch to tenant schema
connection.set_tenant(tenant)

# Create admin user in tenant schema
user, created = User.objects.get_or_create(
    email='yellow@gmail.com',
    defaults={
        'first_name': 'Yellow',
        'last_name': 'Admin',
        'is_staff': True,
        'is_superuser': False,
        'is_active': True,
        'is_verified': True,
        'role': 'admin',
        'company_name': 'Yellow ISP',      # Denormalized
        'tenant_subdomain': 'yellow',       # Denormalized
    }
)
user.set_password('Creative@2028')
user.save()
print(f'Tenant admin created: {created}')
print('Email: yellow@gmail.com')
print('Password: Creative@2028')
print('Access: http://yellow.localhost:3000')
```

> **Note:** Tenant users use `company_name` and `tenant_subdomain` fields (denormalized) instead of foreign keys, since the Company and Tenant models are in the public schema.

---

## Quick Setup Script

For convenience, here's a complete setup script:

```python
#!/usr/bin/env python
"""
Complete database setup script for Netily ISP Management
Run: python setup_database.py
"""

import os
import sys

os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.local'

import django
django.setup()

from django.db import connection
from django.core.management import call_command
from apps.core.models import Company, Tenant, Domain, User


def setup_database():
    print("=" * 60)
    print("NETILY DATABASE SETUP")
    print("=" * 60)
    
    # Step 1: Run shared migrations
    print("\n[1/6] Running shared schema migrations...")
    call_command('migrate_schemas', '--shared')
    
    # Step 2: Create Netily company
    print("\n[2/6] Creating Netily Platform company...")
    netily_company, _ = Company.objects.get_or_create(
        name='Netily Platform',
        defaults={
            'slug': 'netily-platform',
            'company_type': 'isp',
            'email': 'admin@netily.io',
            'phone_number': '+254700000000',
            'address': 'Nairobi, Kenya',
            'city': 'Nairobi',
            'county': 'Nairobi',
            'is_active': True
        }
    )
    print(f"   ✓ Company: {netily_company.name}")
    
    # Step 3: Create public tenant
    print("\n[3/6] Creating public tenant...")
    public_tenant, _ = Tenant.objects.get_or_create(
        schema_name='public',
        defaults={
            'company': netily_company,
            'subdomain': 'public',
            'database_name': 'public',
            'status': 'active',
            'is_active': True
        }
    )
    Domain.objects.get_or_create(
        domain='localhost',
        defaults={'tenant': public_tenant, 'is_primary': True}
    )
    Domain.objects.get_or_create(
        domain='127.0.0.1',
        defaults={'tenant': public_tenant, 'is_primary': False}
    )
    print("   ✓ Public tenant with localhost domain")
    
    # Step 4: Load subscription plans
    print("\n[4/6] Loading subscription plans...")
    call_command('loaddata', 'apps/subscriptions/fixtures/netily_plans.json')
    print("   ✓ 4 subscription plans loaded")
    
    # Step 5: Create superuser
    print("\n[5/6] Creating superuser...")
    admin, created = User.objects.get_or_create(
        email='admin@netily.io',
        defaults={
            'first_name': 'Admin',
            'last_name': 'User',
            'is_staff': True,
            'is_superuser': True,
            'is_active': True,
            'is_verified': True,
            'role': 'admin',
            'company': netily_company,
        }
    )
    admin.set_password('Admin@2026')
    admin.save()
    print("   ✓ Superuser: admin@netily.io / Admin@2026")
    
    # Step 6: Create sample tenant
    print("\n[6/6] Creating sample ISP tenant...")
    sample_company, _ = Company.objects.get_or_create(
        name='Yellow ISP',
        defaults={
            'slug': 'yellow-isp',
            'company_type': 'isp',
            'email': 'admin@yellowisp.com',
            'phone_number': '+254712345678',
            'address': 'Mombasa Road',
            'city': 'Nairobi',
            'county': 'Nairobi',
            'is_active': True
        }
    )
    
    sample_tenant, _ = Tenant.objects.get_or_create(
        subdomain='yellow',
        defaults={
            'schema_name': 'yellow',
            'company': sample_company,
            'database_name': 'yellow',
            'status': 'active',
            'is_active': True
        }
    )
    
    Domain.objects.get_or_create(
        domain='yellow.localhost',
        defaults={'tenant': sample_tenant, 'is_primary': True}
    )
    
    # Create tenant admin user
    connection.set_tenant(sample_tenant)
    tenant_admin, _ = User.objects.get_or_create(
        email='yellow@gmail.com',
        defaults={
            'first_name': 'Yellow',
            'last_name': 'Admin',
            'is_staff': True,
            'is_active': True,
            'is_verified': True,
            'role': 'admin',
            'company_name': 'Yellow ISP',
            'tenant_subdomain': 'yellow',
        }
    )
    tenant_admin.set_password('Creative@2028')
    tenant_admin.save()
    print("   ✓ Yellow ISP tenant: yellow.localhost")
    print("   ✓ Tenant admin: yellow@gmail.com / Creative@2028")
    
    # Summary
    print("\n" + "=" * 60)
    print("SETUP COMPLETE!")
    print("=" * 60)
    print("""
User Accounts:
┌─────────────────────┬─────────────────────┬────────────────────────────┐
│ User                │ Password            │ Access URL                 │
├─────────────────────┼─────────────────────┼────────────────────────────┤
│ admin@netily.io     │ Admin@2026          │ http://localhost:3000      │
│ yellow@gmail.com    │ Creative@2028       │ http://yellow.localhost:3000│
└─────────────────────┴─────────────────────┴────────────────────────────┘

Subscription Plans:
┌─────────────────┬──────────────┬─────────────────────────────────────┐
│ Plan            │ Price/Month  │ Limits                              │
├─────────────────┼──────────────┼─────────────────────────────────────┤
│ Test            │ KES 1        │ 10 subscribers, 1 router, 1 staff   │
│ Starter         │ KES 2,999    │ 100 subscribers, 5 routers, 3 staff │
│ Professional    │ KES 7,999    │ 500 subscribers, 25 routers, 10 staff│
│ Enterprise      │ KES 19,999   │ Unlimited                           │
└─────────────────┴──────────────┴─────────────────────────────────────┘

Start the server:
  python manage.py runserver

Backend API:
  http://localhost:8000/api/v1/
  http://yellow.localhost:8000/api/v1/
""")


if __name__ == '__main__':
    setup_database()
```

Save as `setup_database.py` in the project root and run:
```bash
python setup_database.py
```

---

## Resetting the Database

To completely reset and start fresh:

```bash
# Drop and recreate the database
psql -U postgres -c "DROP DATABASE IF EXISTS isp_management;"
psql -U postgres -c "CREATE DATABASE isp_management;"

# Run the setup
python setup_database.py
```

---

## Troubleshooting

### Missing Columns Error

If you get errors about missing columns (e.g., `company_name`, `tenant_subdomain`):

```python
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.local'
import django
django.setup()

from django.db import connection
from apps.core.models import Tenant

columns = [
    ("company_name", "VARCHAR(255) NULL"),
    ("tenant_subdomain", "VARCHAR(100) NULL"),
]

# Fix public schema
for col, col_type in columns:
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER TABLE public.core_user ADD COLUMN IF NOT EXISTS {col} {col_type}")

# Fix all tenant schemas
for tenant in Tenant.objects.all():
    for col, col_type in columns:
        with connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE {tenant.schema_name}.core_user ADD COLUMN IF NOT EXISTS {col} {col_type}")

print("Columns fixed!")
```

### Tenant Schema Not Created

If tenant migrations don't run automatically:

```bash
python manage.py migrate_schemas --tenant
```

### Foreign Key Errors When Creating Users

Tenant users should NOT have `company` or `tenant` foreign keys set - use the denormalized fields instead:

```python
# ❌ Wrong (cross-schema FK)
user = User.objects.create(
    email='user@example.com',
    company=some_company,  # This will fail!
    tenant=some_tenant,
)

# ✅ Correct (denormalized fields)
user = User.objects.create(
    email='user@example.com',
    company_name='Company Name',
    tenant_subdomain='subdomain',
)
```

---

## Creating New Tenants

To create a new ISP tenant:

```python
from apps.core.models import Company, Tenant, Domain

# 1. Create the company
company = Company.objects.create(
    name='New ISP',
    slug='new-isp',
    company_type='isp',
    email='admin@newisp.com',
    phone_number='+254700000000',
    address='Address',
    city='Nairobi',
    county='Nairobi',
)

# 2. Create the tenant (schema created automatically)
tenant = Tenant.objects.create(
    schema_name='newisp',
    subdomain='newisp',
    company=company,
    database_name='newisp',
    status='active',
)

# 3. Create the domain
Domain.objects.create(
    domain='newisp.localhost',
    tenant=tenant,
    is_primary=True,
)

# 4. Create admin user in tenant schema
from django.db import connection
connection.set_tenant(tenant)

User.objects.create(
    email='admin@newisp.com',
    first_name='Admin',
    last_name='User',
    is_staff=True,
    role='admin',
    company_name='New ISP',
    tenant_subdomain='newisp',
)
```

---

## Database Schema Overview

```
PostgreSQL Database: isp_management
│
├── public (shared schema)
│   ├── core_company          # ISP companies
│   ├── core_tenant           # Tenant configurations
│   ├── core_domain           # Domain-to-tenant mappings
│   ├── core_user             # Platform admin users
│   ├── subscriptions_*       # Netily plans & subscriptions
│   └── auth_*, django_*      # Django system tables
│
├── yellow (tenant schema)
│   ├── core_user             # Tenant-specific users
│   ├── customers_*           # ISP customers
│   ├── billing_*             # Invoices, payments
│   ├── network_*             # Routers, devices
│   └── ...                   # Other tenant data
│
└── [other_tenants]           # Each ISP gets their own schema
```

---

## Environment Variables Reference

```env
# Required
DB_NAME=isp_management
DB_USER=postgres
DB_PASSWORD=your_password
SECRET_KEY=your-secret-key

# Optional
DB_HOST=localhost
DB_PORT=5432
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1

# PayHero (for payments)
PAYHERO_API_USERNAME=your_username
PAYHERO_API_PASSWORD=your_password
PAYHERO_CHANNEL_ID=1114
PAYHERO_ENVIRONMENT=sandbox
```
