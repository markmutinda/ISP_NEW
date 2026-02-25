"""
Create or update the Netily platform superadmin account.

Usage:
    python manage.py create_superadmin
    python manage.py create_superadmin --email admin@netily.co.ke --password MyStr0ng!
"""

from django.core.management.base import BaseCommand
from django.db import connection
from apps.core.models import User


class Command(BaseCommand):
    help = "Create the Netily platform superadmin account"

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            default="admin@netily.co.ke",
            help="Superadmin email address (default: admin@netily.co.ke)",
        )
        parser.add_argument(
            "--password",
            default=None,
            help="Superadmin password. If omitted you will be prompted.",
        )
        parser.add_argument(
            "--first-name",
            default="Netily",
            help="First name (default: Netily)",
        )
        parser.add_argument(
            "--last-name",
            default="Admin",
            help="Last name (default: Admin)",
        )
        parser.add_argument(
            "--noinput",
            action="store_true",
            help="Skip interactive prompts (requires --password)",
        )

    def handle(self, *args, **options):
        # Ensure we're on the public schema
        connection.set_schema_to_public()

        email = options["email"]
        password = options["password"]
        first_name = options["first_name"]
        last_name = options["last_name"]
        noinput = options["noinput"]

        if not password:
            if noinput:
                self.stderr.write(self.style.ERROR("--password is required when using --noinput"))
                return
            import getpass
            password = getpass.getpass("Superadmin password: ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                self.stderr.write(self.style.ERROR("Passwords do not match"))
                return

        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "first_name": first_name,
                "last_name": last_name,
                "role": "admin",
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
                "is_verified": True,
            },
        )

        if created:
            user.set_password(password)
            user.save()
            self.stdout.write(self.style.SUCCESS(f"Superadmin created: {email}"))
        else:
            # Update existing user to ensure superadmin flags
            user.is_superuser = True
            user.is_staff = True
            user.is_active = True
            user.is_verified = True
            user.set_password(password)
            user.save(update_fields=[
                "is_superuser", "is_staff", "is_active", "is_verified", "password",
            ])
            self.stdout.write(self.style.SUCCESS(f"Superadmin updated: {email}"))

        self.stdout.write(
            f"  Email     : {user.email}\n"
            f"  Superuser : {user.is_superuser}\n"
            f"  Staff     : {user.is_staff}\n"
            f"  Verified  : {user.is_verified}\n"
        )
