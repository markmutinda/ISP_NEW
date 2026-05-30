from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models.signals import post_delete, pre_delete


class Command(BaseCommand):
    help = "Force-remove an orphaned tenant schema plus related tenant/company/user records"

    def add_arguments(self, parser):
        parser.add_argument("--schema", required=True, help="Tenant schema name, e.g. tenant_dantenet")
        parser.add_argument("--company", help="Company name to delete, case-insensitive")
        parser.add_argument("--email", help="User email to delete")
        parser.add_argument("--subdomain", help="Tenant subdomain override")
        parser.add_argument("--dry-run", action="store_true", help="Preview actions without deleting anything")

    def handle(self, *args, **options):
        schema_name = options["schema"]
        company_name = options.get("company")
        user_email = options.get("email")
        subdomain = options.get("subdomain") or schema_name.removeprefix("tenant_").replace("_", "-")
        dry_run = options["dry_run"]

        if schema_name == "public":
            raise CommandError("Refusing to purge the public schema.")

        Tenant = apps.get_model("core", "Tenant")
        Company = apps.get_model("core", "Company")
        User = apps.get_model("core", "User")

        self.stdout.write(self.style.NOTICE(f"Preparing purge for schema={schema_name} subdomain={subdomain}"))
        if company_name:
            self.stdout.write(f"Company filter: {company_name}")
        if user_email:
            self.stdout.write(f"User filter: {user_email}")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run only. No changes applied."))
            return

        original_pre_delete = list(pre_delete.receivers)
        original_post_delete = list(post_delete.receivers)
        pre_delete.receivers = []
        post_delete.receivers = []

        try:
            with connection.cursor() as cursor:
                cursor.execute('SET search_path TO "public"')
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
            self.stdout.write(self.style.SUCCESS(f"Dropped schema {schema_name}"))

            deleted_tenant_count, _ = Tenant.objects.filter(schema_name=schema_name).delete()
            self.stdout.write(self.style.SUCCESS(f"Deleted {deleted_tenant_count} tenant record(s)"))

            if company_name:
                deleted_company_count, _ = Company.objects.filter(name__iexact=company_name).delete()
                self.stdout.write(self.style.SUCCESS(f"Deleted {deleted_company_count} company record(s)"))

            if user_email:
                deleted_user_count, _ = User.objects.filter(email=user_email).delete()
                self.stdout.write(self.style.SUCCESS(f"Deleted {deleted_user_count} user record(s)"))
        finally:
            pre_delete.receivers = original_pre_delete
            post_delete.receivers = original_post_delete
