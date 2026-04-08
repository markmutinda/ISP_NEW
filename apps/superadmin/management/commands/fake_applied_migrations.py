"""
Fake all unapplied custom-app migrations by inserting records directly
into django_migrations via raw SQL.

This bypasses Django's consistency check, which would otherwise block
us from faking migrations when the history is inconsistent (e.g. after
purging a parallel migration branch).

SAFE when all DB changes already exist from a parallel migration branch.
NO tables or data are touched — only django_migrations records.
"""
import os
from django.core.management.base import BaseCommand
from django.db import connection
from django.conf import settings


class Command(BaseCommand):
    help = (
        "Fake all unapplied migrations for custom apps via raw SQL. "
        "Use after purging a parallel branch when DB state already matches."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Preview what would be faked without inserting records',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        if dry_run:
            self.stdout.write(self.style.WARNING("══════ DRY RUN ══════\n"))

        shared_labels = self._custom_app_labels(settings.SHARED_APPS)
        tenant_labels = self._custom_app_labels(settings.TENANT_APPS)

        self.stdout.write(f"Shared custom apps: {', '.join(sorted(shared_labels))}")
        self.stdout.write(f"Tenant custom apps: {', '.join(sorted(tenant_labels))}")

        schemas = self._get_schemas()
        self.stdout.write(f"Schemas: {', '.join(schemas)}\n")

        grand_total = 0
        for schema in schemas:
            is_public = schema == 'public'
            labels = shared_labels if is_public else tenant_labels
            faked = self._fake_for_schema(schema, labels, dry_run)
            grand_total += faked

        verb = "Would fake" if dry_run else "Faked"
        self.stdout.write(self.style.SUCCESS(
            f"\n══════ DONE — {verb} {grand_total} migration(s) total ══════"
        ))

        if not dry_run and grand_total > 0:
            self.stdout.write(
                "\nNext steps:\n"
                "  python manage.py migrate_schemas --shared\n"
                "  python manage.py migrate_schemas --tenant\n"
            )

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _custom_app_labels(apps_tuple):
        """Extract labels for custom apps (apps.*) from a SHARED/TENANT tuple."""
        labels = set()
        for entry in apps_tuple:
            if entry.startswith('apps.'):
                labels.add(entry.rsplit('.', 1)[-1])
        return labels

    def _get_schemas(self):
        schemas = ['public']
        with connection.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast')"
            )
            existing = {row[0] for row in cur.fetchall()}

        from apps.core.models import Tenant
        for s in Tenant.objects.values_list('schema_name', flat=True):
            if s in existing:
                schemas.append(s)
        return schemas

    def _fake_for_schema(self, schema, app_labels, dry_run):
        faked = 0
        for app_label in sorted(app_labels):
            migrations_dir = os.path.join(
                settings.BASE_DIR, 'apps', app_label, 'migrations',
            )
            if not os.path.isdir(migrations_dir):
                continue

            names = sorted(
                f[:-3]
                for f in os.listdir(migrations_dir)
                if f.endswith('.py') and f != '__init__.py'
            )

            with connection.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
                for name in names:
                    cur.execute(
                        "SELECT 1 FROM django_migrations "
                        "WHERE app = %s AND name = %s LIMIT 1",
                        [app_label, name],
                    )
                    if cur.fetchone():
                        continue  # already recorded

                    if dry_run:
                        self.stdout.write(
                            f"  [{schema}] Would fake: {app_label}.{name}"
                        )
                    else:
                        cur.execute(
                            "INSERT INTO django_migrations (app, name, applied) "
                            "VALUES (%s, %s, NOW())",
                            [app_label, name],
                        )
                        self.stdout.write(self.style.SUCCESS(
                            f"  [{schema}] Faked: {app_label}.{name}"
                        ))
                    faked += 1

        if faked:
            verb = "Would fake" if dry_run else "Faked"
            self.stdout.write(f"  ── {verb} {faked} in {schema}\n")

        return faked
