"""
Idempotent command to ensure the platform superadmin account exists.
Safe to run on every deploy — does nothing if the account already exists.
Credentials are read from environment variables so they're never hardcoded.

Required env vars (with fallback defaults for bootstrapping):
  SUPERADMIN_EMAIL    default: admin@netily.co.ke
  SUPERADMIN_PASSWORD default: (must be set explicitly in production)
"""
import os
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django_tenants.utils import schema_context

User = get_user_model()


class Command(BaseCommand):
    help = "Ensure platform superadmin account exists (idempotent)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            default=os.environ.get("SUPERADMIN_EMAIL", "admin@netily.co.ke"),
            help="Superadmin email address",
        )
        parser.add_argument(
            "--password",
            default=os.environ.get("SUPERADMIN_PASSWORD", ""),
            help="Superadmin password (required if account does not exist)",
        )
        parser.add_argument(
            "--first-name",
            default=os.environ.get("SUPERADMIN_FIRST_NAME", "Netily"),
            help="First name",
        )
        parser.add_argument(
            "--last-name",
            default=os.environ.get("SUPERADMIN_LAST_NAME", "Admin"),
            help="Last name",
        )

    def handle(self, *args, **options):
        email = options["email"]
        password = options["password"]
        first_name = options["first_name"]
        last_name = options["last_name"]

        # Superadmin lives in the public schema
        with schema_context("public"):
            if User.objects.filter(email=email).exists():
                user = User.objects.get(email=email)
                # Ensure flags are correct even if account pre-existed
                changed = False
                if not user.is_superuser:
                    user.is_superuser = True
                    changed = True
                if not user.is_staff:
                    user.is_staff = True
                    changed = True
                if not user.is_active:
                    user.is_active = True
                    changed = True
                if changed:
                    user.save(update_fields=["is_superuser", "is_staff", "is_active"])
                    self.stdout.write(
                        self.style.WARNING(f"Superadmin {email} already existed — flags updated.")
                    )
                else:
                    self.stdout.write(
                        self.style.SUCCESS(f"Superadmin {email} already exists — no changes needed.")
                    )
                return

            if not password:
                self.stderr.write(
                    self.style.ERROR(
                        "SUPERADMIN_PASSWORD env var is not set and --password was not provided. "
                        "Cannot create superadmin account."
                    )
                )
                raise SystemExit(1)

            try:
                user = User.objects.create_superuser(
                    email=email,
                    password=password,
                    first_name=first_name,
                    last_name=last_name,
                    is_verified=True,
                )
                # Set role if the field exists
                if hasattr(user, "role"):
                    user.role = "admin"
                    user.save(update_fields=["role"])

                self.stdout.write(
                    self.style.SUCCESS(f"Superadmin account created: {email}")
                )
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"Failed to create superadmin: {exc}"))
                raise
