"""Resolve one tenant account owner for platform billing reminders."""

import re

from django.db.models import Q
from django_tenants.utils import get_public_schema_name, schema_context

from apps.core.models import AuditLog, User


SYSTEM_EMAILS = {'admin@netily.co.ke', 'admin@netily.io', 'billing@netily.io'}


def normalize_billing_phone(value):
    raw = str(value or '').strip()
    if not re.fullmatch(r'\+?[\d\s()-]+', raw):
        return None
    digits = re.sub(r'\D', '', raw)
    if digits.startswith('0') and len(digits) == 10:
        digits = '254' + digits[1:]
    elif len(digits) == 9 and digits[0] in '17':
        digits = '254' + digits
    if digits == '254700000001':
        return None
    if digits.startswith('254'):
        return '+' + digits if re.fullmatch(r'254[17]\d{8}', digits) else None
    return '+' + digits if raw.startswith('+') and re.fullmatch(r'[1-9]\d{7,14}', digits) else None


def _eligible(user, tenant):
    # Signup grants tenant owners is_superuser=True inside their own schema.
    # Platform identity is determined by role and configured identities below.
    return bool(
        user.is_active
        and user.role in {'admin', 'owner'}
        and user.company_id in {None, tenant.company_id}
        and user.tenant_id in {None, tenant.pk}
        and str(user.email or '').strip().lower() not in SYSTEM_EMAILS
        and not AuditLog._is_platform_superadmin_actor(user)
    )


def _select_recipient(tenant, signup_users, local_users):
    # Public signup records identify ownership; tenant-local copies hold current
    # contact details and the correct ID for tenant in-app notifications.
    signup = next((user for user in signup_users if _eligible(user, tenant)), None)
    selected = None
    local_id = None
    if signup:
        email = str(signup.email or '').strip().lower()
        mirror = next((user for user in local_users if email and str(user.email or '').strip().lower() == email), None)
        if mirror:
            if not _eligible(mirror, tenant):
                return []
            selected, local_id = mirror, mirror.pk
        else:
            selected = signup
    else:
        selected = next((user for user in local_users if _eligible(user, tenant)), None)
        if selected:
            local_id = selected.pk
    if not selected:
        return []
    return [{
        'id': local_id,
        'email': selected.email or '',
        'phone_number': normalize_billing_phone(selected.phone_number),
        'first_name': selected.first_name,
        'last_name': selected.last_name,
    }]


def tenant_billing_recipients(tenant):
    if tenant.schema_name == get_public_schema_name():
        return []
    with schema_context(get_public_schema_name()):
        signup_users = list(User.objects.filter(
            company_id=tenant.company_id, role__in=['admin', 'owner'], is_active=True,
        ).order_by('date_joined', 'pk'))
    with schema_context(tenant.schema_name):
        local_users = list(User.objects.filter(
            Q(company_id=tenant.company_id) | Q(company_id__isnull=True),
            Q(tenant_id=tenant.pk) | Q(tenant_id__isnull=True),
        ).filter(role__in=['admin', 'owner']).order_by('date_joined', 'pk'))
    return _select_recipient(tenant, signup_users, local_users)
