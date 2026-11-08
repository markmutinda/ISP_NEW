"""Helpers for child accounts that share a parent's phone number."""

CHILD_SUFFIXES = "BCDEFGHIJKLMNOPQRSTUVWXYZ"   # parent has no suffix


def local_phone(raw):
    """+254712345678 / 254712345678 / 0712345678 -> 0712345678 (same logic as the billing signal)."""
    digits = ''.join(ch for ch in str(raw or '') if ch.isdigit())
    if digits.startswith('254') and len(digits) >= 12:
        return '0' + digits[3:]
    if digits.startswith('0') and len(digits) >= 10:
        return digits
    return digits[-10:] if len(digits) >= 10 else digits


def radius_base_username(raw):
    return local_phone(raw)[-9:]          # 712345678


def next_child_suffix(root, base_local, base_username):
    """First free letter for sibling suffix, billing account AND RADIUS username."""
    from apps.customers.models import Customer, ServiceConnection
    from apps.radius.models import CustomerRadiusCredentials

    taken = set(Customer.objects.filter(parent=root).values_list('account_suffix', flat=True))
    accounts = {
        a.upper() for a in ServiceConnection.objects
        .filter(billing_account_number__startswith=base_local)
        .values_list('billing_account_number', flat=True)
    }
    usernames = {
        u.upper() for u in CustomerRadiusCredentials.objects
        .filter(username__startswith=base_username)
        .values_list('username', flat=True)
    }
    for s in CHILD_SUFFIXES:
        if s in taken or f"{base_local}{s}" in accounts or f"{base_username}{s}" in usernames:
            continue
        return s
    return None