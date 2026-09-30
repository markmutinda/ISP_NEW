"""
Generates unique per-device ACS credentials and keeps the public
Tr069DeviceIndex (used by the GenieACS ext auth script) in sync with the
tenant-local CPEDevice record. Mirrors how Router.shared_secret is mirrored
into GlobalRouterMap for RADIUS.
"""
import secrets

from django.conf import settings
from django.db import connection
from django_tenants.utils import get_public_schema_name, schema_context

from apps.core.models import Tenant, Tr069DeviceIndex


def generate_credentials() -> tuple[str, str]:
    return secrets.token_urlsafe(18)[:24], secrets.token_urlsafe(18)[:24]


def sync_index_entry(device, *, is_active=True):
    """Call after creating/updating/rotating a CPEDevice's credentials."""
    tenant_schema = connection.schema_name
    with schema_context(get_public_schema_name()):
        tenant = Tenant.objects.filter(schema_name=tenant_schema).first()
        if not tenant:
            return
        Tr069DeviceIndex.objects.update_or_create(
            serial_number=device.serial_number,
            defaults={
                'genieacs_device_id': device.genieacs_device_id or None,
                'acs_username': device.acs_username,
                'acs_password': device.acs_password,
                'tenant': tenant,
                'tenant_schema': tenant_schema,
                'is_active': is_active,
            },
        )


def deactivate_index_entry(serial_number: str):
    with schema_context(get_public_schema_name()):
        Tr069DeviceIndex.objects.filter(serial_number=serial_number).update(is_active=False)


def acs_url() -> str:
    return getattr(settings, 'TR069_ACS_URL', 'http://acs.netily.co.ke/')