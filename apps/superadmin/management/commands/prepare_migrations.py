"""
Prepare migration history before running migrate_schemas in production.

This command fixes the two classes of issues that keep showing up on VPS
deployments:

1. Stale migration records left behind by old/removed migration files.
2. Missing dependency records after branch cleanup or manual repair.

It is intentionally conservative:
- It only touches schemas that actually exist.
- It skips schemas with no django_migrations table.
- It only deletes migration records for files that do not exist on disk.

Recommended usage:
    python manage.py prepare_migrations
    python manage.py migrate_schemas --shared
    python manage.py migrate_schemas --tenant
"""

from __future__ import annotations

import importlib
import os

from django.apps import apps as django_apps
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Clean stale migration records and repair inconsistent history before migrations"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview stale migration records without deleting them.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        disk_migrations = self._get_disk_migrations()
        schemas = self._get_existing_schemas()

        total_deleted = 0
        for schema in schemas:
            total_deleted += self._clean_schema(schema, disk_migrations, dry_run=dry_run)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDry run complete. {total_deleted} stale migration record(s) would be removed."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"\nRemoved {total_deleted} stale migration record(s). Repairing missing dependencies..."
            )
        )
        call_command("repair_migrations", all_schemas=True)
        self.stdout.write(self.style.SUCCESS("Migration preparation complete."))

    def _get_disk_migrations(self) -> set[tuple[str, str]]:
        disk_migrations: set[tuple[str, str]] = set()

        for app_config in django_apps.get_app_configs():
            try:
                migrations_module_name = f"{app_config.name}.migrations"
                migrations_module = importlib.import_module(migrations_module_name)
                migrations_path = os.path.dirname(migrations_module.__file__)
            except (ImportError, AttributeError, TypeError):
                continue

            for filename in os.listdir(migrations_path):
                if filename.endswith(".py") and not filename.startswith("_"):
                    disk_migrations.add((app_config.label, filename[:-3]))

        return disk_migrations

    def _get_existing_schemas(self) -> list[str]:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT schema_name
                FROM information_schema.schemata
                WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast')
                ORDER BY schema_name
                """
            )
            return [row[0] for row in cursor.fetchall()]

    def _clean_schema(
        self,
        schema: str,
        disk_migrations: set[tuple[str, str]],
        *,
        dry_run: bool,
    ) -> int:
        deleted = 0
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_schema = %s
                      AND table_name = 'django_migrations'
                )
                """,
                [schema],
            )
            if not cursor.fetchone()[0]:
                self.stdout.write(self.style.WARNING(f"Skipping {schema}: no django_migrations table"))
                return 0

            cursor.execute(f'SET search_path TO "{schema}"')
            cursor.execute("SELECT app, name FROM django_migrations")
            applied = cursor.fetchall()

            stale = sorted((app, name) for app, name in applied if (app, name) not in disk_migrations)

            if not stale:
                self.stdout.write(self.style.SUCCESS(f"{schema}: no stale migration records"))
                cursor.execute('SET search_path TO "public"')
                return 0

            self.stdout.write(
                self.style.WARNING(
                    f"{schema}: found {len(stale)} stale migration record(s): "
                    + ", ".join(f"{app}.{name}" for app, name in stale)
                )
            )

            if not dry_run:
                for app, name in stale:
                    cursor.execute(
                        "DELETE FROM django_migrations WHERE app = %s AND name = %s",
                        [app, name],
                    )
                    deleted += cursor.rowcount
            else:
                deleted += len(stale)

            cursor.execute('SET search_path TO "public"')

        return deleted
