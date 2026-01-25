
import os
from dotenv import load_dotenv
load_dotenv()
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.local'
import django
django.setup()

from django.utils.text import slugify
from apps.core.models import Company, Tenant, Domain

print("DB_PASSWORD from env:", os.environ.get('DB_PASSWORD'))  # ← debug line
print("DB_USER from env:", os.environ.get('DB_USER'))

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