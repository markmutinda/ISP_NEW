def ensure_technician_profile(user):
    """Create/reactivate the dispatch Technician row for a technician-role user."""
    from .models import Technician

    tech, created = Technician.objects.get_or_create(
        user=user,
        defaults={
            "employee_id": f"STF{user.pk:05d}",
            "phone": (user.phone_number or "")[:20],
            "status": "available",
        },
    )
    if not created and not tech.is_active:
        tech.is_active = True
        tech.save(update_fields=["is_active"])
    return tech


def deactivate_technician_profile(user):
    from .models import Technician

    Technician.objects.filter(user=user, is_active=True).update(is_active=False)