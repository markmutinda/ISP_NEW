"""
Management command to fix tenant domains and repair orphaned schemas.

Usage:
    # List all tenants and their domains:
    python manage.py fix_tenant_domains --dry-run

    # Fix domains from *.localhost to *.netily.co.ke:
    python manage.py fix_tenant_domains --base-domain netily.co.ke

    # List tenants with missing schemas:
    python manage.py fix_tenant_domains --check-schemas
"""

import logging
from django.core.management.base import BaseCommand
from django.db import connection

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Fix tenant domains and check for orphaned schemas"

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-domain",
            type=str,
            help="Target base domain (e.g. 'netily.co.ke'). Rewrites all primary domains to subdomain.base_domain",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print changes without applying them",
        )
        parser.add_argument(
            "--check-schemas",
            action="store_true",
            help="Check for tenants whose PostgreSQL schema does not exist",
        )
        parser.add_argument(
            "--delete-orphans",
            action="store_true",
            help="Delete tenants whose PostgreSQL schema does not exist (irreversible unless --dry-run)",
        )

    def handle(self, *args, **options):
        from apps.core.models import Tenant, Domain

        connection.set_schema_to_public()

        base_domain = options.get("base_domain")
        dry_run = options.get("dry_run")
        check_schemas = options.get("check_schemas")
        delete_orphans = options.get("delete_orphans")

        tenants = Tenant.objects.select_related("company").all()

        if check_schemas or delete_orphans or (not base_domain):
            self.stdout.write(self.style.MIGRATE_HEADING("\n=== Tenant Schema Check ==="))
            existing_schemas = set()
            with connection.cursor() as cur:
                cur.execute("SELECT schema_name FROM information_schema.schemata")
                existing_schemas = {row[0] for row in cur.fetchall()}

            orphans = []
            for tenant in tenants:
                domain = tenant.domains.filter(is_primary=True).first()
                domain_str = domain.domain if domain else "(no domain)"
                schema_exists = tenant.schema_name in existing_schemas
                status_icon = "✓" if schema_exists else "✗ MISSING"

                self.stdout.write(
                    f"  [{status_icon}] {tenant.subdomain} | schema={tenant.schema_name} | "
                    f"domain={domain_str} | status={tenant.status}"
                )

                if not schema_exists:
                    orphans.append(tenant)
                    self.stdout.write(
                        self.style.WARNING(
                            f"    ⚠ Schema '{tenant.schema_name}' does not exist in PostgreSQL. "
                            f"Migrations will fail for this tenant. Consider:\n"
                            f"      1. Delete this tenant if it's no longer needed\n"
                            f"      2. Run: python manage.py migrate_schemas --schema={tenant.schema_name}\n"
                            f"      3. Or set status to 'cancelled' to skip during migrations"
                        )
                    )

            if delete_orphans and orphans:
                self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== Deleting {len(orphans)} Orphaned Tenant(s) ==="))
                for tenant in orphans:
                    if dry_run:
                        self.stdout.write(
                            self.style.WARNING(
                                f"  DRY RUN: Would delete tenant '{tenant.subdomain}' (schema={tenant.schema_name})"
                            )
                        )
                    else:
                        tenant.domains.all().delete()
                        tenant.delete()
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"  Deleted orphaned tenant '{tenant.subdomain}' (schema={tenant.schema_name})"
                            )
                        )
                if not dry_run:
                    self.stdout.write(self.style.SUCCESS(f"\nDeleted {len(orphans)} orphaned tenant(s)."))
            elif delete_orphans and not orphans:
                self.stdout.write(self.style.SUCCESS("\nNo orphaned tenants found. Nothing to delete."))

        if base_domain:
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== Rewriting Domains to *.{base_domain} ==="))
            updated = 0
            for tenant in tenants:
                domain = tenant.domains.filter(is_primary=True).first()
                if not domain:
                    self.stdout.write(
                        self.style.WARNING(f"  {tenant.subdomain}: No primary domain found, skipping")
                    )
                    continue

                new_domain = f"{tenant.subdomain}.{base_domain}"
                if domain.domain == new_domain:
                    self.stdout.write(f"  {tenant.subdomain}: Already correct ({new_domain})")
                    continue

                old = domain.domain
                if dry_run:
                    self.stdout.write(f"  {tenant.subdomain}: {old} → {new_domain} (DRY RUN)")
                else:
                    domain.domain = new_domain
                    domain.save()
                    self.stdout.write(self.style.SUCCESS(f"  {tenant.subdomain}: {old} → {new_domain}"))
                updated += 1

            action = "would update" if dry_run else "updated"
            self.stdout.write(self.style.SUCCESS(f"\n{action} {updated} domain(s)."))

        if not base_domain and not check_schemas and not delete_orphans:
            self.stdout.write("\nUse --base-domain to fix domains, --check-schemas to check for orphans, or --delete-orphans to remove them.")

