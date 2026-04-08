"""
Data migration: Create the Netily platform superadmin account.

This ensures every developer who runs `python manage.py migrate` gets:
  - Email:    admin@netily.co.ke
  - Password: Netily@2024!
  - is_superuser=True, is_staff=True, is_verified=True, role=admin

The management command `create_superadmin` can be used afterwards
to change the password or email.
"""
from django.db import migrations


def create_superadmin(apps, schema_editor):
    User = apps.get_model("core", "User")
    db_alias = schema_editor.connection.alias

    # Only create if not already present
    if User.objects.using(db_alias).filter(email="admin@netily.co.ke").exists():
        return

    # Check for legacy email too
    if User.objects.using(db_alias).filter(email="admin@netily.io").exists():
        user = User.objects.using(db_alias).get(email="admin@netily.io")
        user.email = "admin@netily.co.ke"
        user.is_superuser = True
        user.is_staff = True
        user.is_active = True
        user.is_verified = True
        user.first_name = "Netily"
        user.last_name = "Admin"
        user.save(using=db_alias)
        return

    # Handle phone_number unique constraint — generate a unique placeholder
    phone = "+254700000001"
    while User.objects.using(db_alias).filter(phone_number=phone).exists():
        num = int(phone.replace("+", "")) + 1
        phone = f"+{num}"

    user = User(
        email="admin@netily.co.ke",
        phone_number=phone,
        first_name="Netily",
        last_name="Admin",
        role="admin",
        is_staff=True,
        is_superuser=True,
        is_active=True,
        is_verified=True,
    )
    # set_password not available in data migrations — use Django's make_password
    from django.contrib.auth.hashers import make_password
    user.password = make_password("Netily@2024!")
    user.save(using=db_alias)


def remove_superadmin(apps, schema_editor):
    User = apps.get_model("core", "User")
    User.objects.using(schema_editor.connection.alias).filter(
        email="admin@netily.co.ke", is_superuser=True
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_superadmin, remove_superadmin),
    ]
