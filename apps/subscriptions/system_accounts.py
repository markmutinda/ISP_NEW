"""Tenant-local system identities used by subscription billing."""

from django.contrib.auth import get_user_model


SYSTEM_BILLING_EMAIL = "billing@netily.io"
SYSTEM_BILLING_CUSTOMER_CODE = "NET-001"


def get_tenant_billing_customer():
    """Return a non-login customer identity for NET-BILL invoice ownership."""
    from apps.customers.models import Customer

    User = get_user_model()
    user, _ = User.objects.get_or_create(
        email=SYSTEM_BILLING_EMAIL,
        defaults={
            "first_name": "Netily",
            "last_name": "Platform",
            "role": "customer",
            "is_staff": False,
            "is_superuser": False,
            "is_active": False,
            "is_verified": False,
        },
    )

    changed_fields = []
    expected = {
        "first_name": "Netily",
        "last_name": "Platform",
        "role": "customer",
        "is_staff": False,
        "is_superuser": False,
        "is_active": False,
        "is_verified": False,
        "custom_allowed_paths": None,
    }
    for field, value in expected.items():
        if getattr(user, field) != value:
            setattr(user, field, value)
            changed_fields.append(field)
    if user.has_usable_password():
        user.set_unusable_password()
        changed_fields.append("password")
    if changed_fields:
        changed_fields.append("updated_at")
        user.save(update_fields=list(dict.fromkeys(changed_fields)))

    customer, _ = Customer.objects.get_or_create(
        customer_code=SYSTEM_BILLING_CUSTOMER_CODE,
        defaults={"user": user, "status": "active"},
    )
    if customer.user_id != user.id:
        customer.user = user
        customer.save(update_fields=["user"])
    return customer
