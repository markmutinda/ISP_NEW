# test_network.py
import os
import django
import sys

# Add the project to the Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')
django.setup()

from apps.network.models.olt_models import OLTDevice
from apps.network.models.ipam_models import Subnet
from apps.core.models import Company

# Test creating a company if none exists
company = Company.objects.first()

if not company:
    print("No company found. Creating a test company...")
    company = Company.objects.create(
        name="Test ISP Company",
        slug="test-isp",
        email="test@isp.com",
        phone_number="+254712345678",
        address="Nairobi, Kenya",
        city="Nairobi",
        registration_number="TEST001"
    )
    print(f"Created company: {company}")
else:
    print(f"Using existing company: {company}")

# Test OLT Device
try:
    olt = OLTDevice.objects.create(
        company=company,
        name="Test OLT",
        hostname="olt1.isp.local",
        ip_address="192.168.1.10",
        vendor="ZTE",
        model="ZXA10 C300",
        serial_number="TEST123456",
        status="ACTIVE"
    )
    print(f"✓ Created OLT: {olt}")
except Exception as e:
    print(f"✗ Failed to create OLT: {e}")

# Test Subnet
try:
    subnet = Subnet.objects.create(
        company=company,
        name="Test Subnet",
        network_address="192.168.100.0",
        subnet_mask="255.255.255.0",
        cidr="24",
        total_ips=254,
        used_ips=0,
        available_ips=254
    )
    print(f"✓ Created Subnet: {subnet.network_address}/{subnet.cidr}")
except Exception as e:
    print(f"✗ Failed to create subnet: {e}")

print("\n✅ Test completed!")