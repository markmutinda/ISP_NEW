# apps/customers/billing_account.py
"""
Utility for generating unique billing account numbers for PPPoE/Static customers.
These are used as the M-Pesa Paybill account reference for C2B payments.
"""

from django.db.models import Max
import re


def generate_billing_account_number(customer, service_connection=None):
    """
    Generate a unique billing account number for a service connection.
    
    Format: {CUSTOMER_CODE}-{SEQUENTIAL_NUMBER}
    Example: JOH-001, TRI-002, CUST-001
    
    The account number is short enough for customers to type as the M-Pesa
    Paybill account reference (max 12 chars, alphanumeric + hyphen).
    """
    from apps.customers.models import ServiceConnection

    # Use the customer code prefix (first 3-5 chars, alphanumeric only)
    raw_code = customer.customer_code or customer.customer_number or f"C{customer.id}"
    # Strip non-alphanumeric, uppercase, max 5 chars
    prefix = re.sub(r'[^A-Z0-9]', '', raw_code.upper())[:5] or 'ACC'

    # Find the highest existing sequential number for this prefix
    existing = ServiceConnection.objects.filter(
        billing_account_number__startswith=f"{prefix}-"
    ).values_list('billing_account_number', flat=True)

    max_seq = 0
    for acc in existing:
        parts = acc.split('-')
        if len(parts) >= 2:
            try:
                seq = int(parts[-1])
                if seq > max_seq:
                    max_seq = seq
            except ValueError:
                pass

    new_seq = max_seq + 1
    return f"{prefix}-{new_seq:03d}"


def format_billing_account_for_display(billing_account_number):
    """Return a display-friendly version of the billing account number."""
    return billing_account_number.upper() if billing_account_number else None