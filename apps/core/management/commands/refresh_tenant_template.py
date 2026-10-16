from django.core.management.base import BaseCommand
from apps.core.tenant_template import refresh_template, pending_migrations, TEMPLATE_SCHEMA
from django_tenants.utils import schema_exists


class Command(BaseCommand):
    help = "Create/update the template_tenant schema used for fast tenant cloning."

    def add_arguments(self, parser):
        parser.add_argument("--rebuild", action="store_true", help="Drop and recreate from scratch.")
        parser.add_argument("--check", action="store_true", help="Only report pending migrations.")

    def handle(self, *args, **opts):
        if opts["check"]:
            if not schema_exists(TEMPLATE_SCHEMA):
                self.stdout.write(self.style.WARNING("Template does not exist."))
                return
            plan = pending_migrations()
            self.stdout.write(f"Pending on template: {len(plan)}")
            for migration, _ in plan:
                self.stdout.write(f"  - {migration}")
            return
        info = refresh_template(rebuild=opts["rebuild"])
        self.stdout.write(self.style.SUCCESS(f"Template ready: {info}"))