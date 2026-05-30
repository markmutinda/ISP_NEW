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
        existing_schema_set = set(schemas)

        total_deleted = 0
        total_faked = 0
        total_unapplied = 0
        total_pruned = self._prune_orphan_tenant_records(existing_schema_set, dry_run=dry_run)
        for schema in schemas:
            total_deleted += self._clean_schema(schema, disk_migrations, dry_run=dry_run)
            total_unapplied += self._unheal_false_applied_migrations(schema, dry_run=dry_run)
            total_faked += self._heal_preapplied_migrations(schema, dry_run=dry_run)
            total_faked += self._heal_missing_dependencies(schema, disk_migrations, dry_run=dry_run)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDry run complete. {total_pruned} orphan tenant record(s) would be removed, "
                    f"{total_deleted} stale migration record(s) would be removed, "
                    f"{total_unapplied} false-applied migration record(s) would be removed, and "
                    f"{total_faked} migration record(s) would be faked."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"\nRemoved {total_pruned} orphan tenant record(s), removed {total_deleted} stale migration "
                f"record(s), removed {total_unapplied} "
                f"false-applied migration record(s), and faked {total_faked} migration record(s). "
                f"Repairing any remaining inconsistencies..."
            )
        )
        call_command("repair_migrations", all_schemas=True)
        self.stdout.write(self.style.SUCCESS("Migration preparation complete."))

    def _prune_orphan_tenant_records(self, existing_schemas: set[str], *, dry_run: bool) -> int:
        Tenant = django_apps.get_model("core", "Tenant")
        Domain = django_apps.get_model("core", "Domain")

        orphaned = list(
            Tenant.objects.exclude(schema_name="public").exclude(schema_name__in=existing_schemas)
        )
        if not orphaned:
            self.stdout.write(self.style.SUCCESS("No orphan tenant records found"))
            return 0

        self.stdout.write(
            self.style.WARNING(
                "Found orphan tenant record(s) with missing schema: "
                + ", ".join(f"{tenant.subdomain} ({tenant.schema_name})" for tenant in orphaned)
            )
        )

        if dry_run:
            return len(orphaned)

        pruned = 0
        for tenant in orphaned:
            Domain.objects.filter(tenant=tenant).delete()
            deleted, _ = Tenant.objects.filter(pk=tenant.pk).delete()
            pruned += deleted
            self.stdout.write(
                self.style.WARNING(
                    f"Pruned orphan tenant '{tenant.subdomain}' with missing schema '{tenant.schema_name}'"
                )
            )

        return pruned

    def _get_disk_migrations(self) -> dict[tuple[str, str], object]:
        disk_migrations: dict[tuple[str, str], object] = {}

        for app_config in django_apps.get_app_configs():
            try:
                migrations_module_name = f"{app_config.name}.migrations"
                migrations_pkg = importlib.import_module(migrations_module_name)
                migrations_path = os.path.dirname(migrations_pkg.__file__)
            except (ImportError, AttributeError, TypeError):
                continue

            for filename in os.listdir(migrations_path):
                if filename.endswith(".py") and not filename.startswith("_"):
                    name = filename[:-3]
                    try:
                        mod = importlib.import_module(f"{migrations_module_name}.{name}")
                    except Exception:
                        continue
                    disk_migrations[(app_config.label, name)] = mod

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
        disk_migrations: dict[tuple[str, str], object],
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

    def _heal_missing_dependencies(
        self,
        schema: str,
        disk_migrations: dict[tuple[str, str], object],
        *,
        dry_run: bool,
    ) -> int:
        def get_dependencies(migration_module):
            migration_cls = getattr(migration_module, "Migration", None)
            if migration_cls is None:
                return []
            return getattr(migration_cls, "dependencies", [])

        healed = 0
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
                return 0

            cursor.execute(f'SET search_path TO "{schema}"')
            cursor.execute("SELECT app, name FROM django_migrations")
            applied = {(app, name) for app, name in cursor.fetchall()}

            to_fake: set[tuple[str, str]] = set()
            changed = True
            while changed:
                changed = False
                for migration_key in list(applied) + list(to_fake):
                    mod = disk_migrations.get(migration_key)
                    if mod is None:
                        continue
                    for dep_app, dep_name in get_dependencies(mod):
                        dep_key = (dep_app, dep_name)
                        if dep_key in applied or dep_key in to_fake:
                            continue
                        if dep_key in disk_migrations:
                            to_fake.add(dep_key)
                            changed = True

            if to_fake:
                names = ", ".join(f"{app}.{name}" for app, name in sorted(to_fake))
                self.stdout.write(self.style.WARNING(f"{schema}: missing dependency record(s): {names}"))

            if not dry_run:
                for app, name in sorted(to_fake):
                    cursor.execute(
                        "INSERT INTO django_migrations (app, name, applied) VALUES (%s, %s, NOW()) "
                        "ON CONFLICT DO NOTHING",
                        [app, name],
                    )
                    healed += cursor.rowcount
            else:
                healed += len(to_fake)

            cursor.execute('SET search_path TO "public"')

        return healed

    def _heal_preapplied_migrations(self, schema: str, *, dry_run: bool) -> int:
        healed = 0
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
                return 0

            cursor.execute(f'SET search_path TO "{schema}"')
            cursor.execute("SELECT app, name FROM django_migrations")
            applied = {(app, name) for app, name in cursor.fetchall()}

            candidates: list[tuple[str, str]] = []

            inventory_0002 = ("inventory", "0002_alter_equipmentitem_options_and_more")
            if inventory_0002 not in applied and self._schema_matches_inventory_0002(cursor, schema):
                candidates.append(inventory_0002)

            network_0014 = ("network", "0014_router_api_remote_port_router_winbox_remote_port_and_more")
            if network_0014 not in applied and self._schema_matches_network_0014(cursor, schema):
                candidates.append(network_0014)

            if candidates:
                names = ", ".join(f"{app}.{name}" for app, name in candidates)
                self.stdout.write(self.style.WARNING(f"{schema}: pre-applied migration record(s): {names}"))

            if not dry_run:
                for app, name in candidates:
                    cursor.execute(
                        "INSERT INTO django_migrations (app, name, applied) VALUES (%s, %s, NOW()) "
                        "ON CONFLICT DO NOTHING",
                        [app, name],
                    )
                    healed += cursor.rowcount
            else:
                healed += len(candidates)

            cursor.execute('SET search_path TO "public"')

        return healed

    def _unheal_false_applied_migrations(self, schema: str, *, dry_run: bool) -> int:
        removed = 0
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
                return 0

            cursor.execute(f'SET search_path TO "{schema}"')
            cursor.execute("SELECT app, name FROM django_migrations")
            applied = {(app, name) for app, name in cursor.fetchall()}

            candidates: list[tuple[str, str]] = []

            core_0014 = ("core", "0014_lead_contacted_fields")
            if core_0014 in applied and not self._schema_matches_core_0014(cursor, schema):
                candidates.append(core_0014)

            if candidates:
                names = ", ".join(f"{app}.{name}" for app, name in candidates)
                self.stdout.write(self.style.WARNING(f"{schema}: false-applied migration record(s): {names}"))

            if not dry_run:
                for app, name in candidates:
                    cursor.execute(
                        "DELETE FROM django_migrations WHERE app = %s AND name = %s",
                        [app, name],
                    )
                    removed += cursor.rowcount
            else:
                removed += len(candidates)

            cursor.execute('SET search_path TO "public"')

        return removed

    def _schema_matches_inventory_0002(self, cursor, schema: str) -> bool:
        required_columns = {
            "inventory_assignment": {"created_at", "updated_at"},
            "inventory_equipmentitem": {"assigned_to_customer_id", "created_at", "updated_at"},
            "inventory_equipmenttype": {"code", "created_at", "is_active", "min_stock_level", "updated_at"},
            "inventory_maintenancerecord": {"created_at", "updated_at"},
            "inventory_purchaseorder": {"created_at", "updated_at"},
            "inventory_purchaseorderitem": {"created_at", "updated_at"},
            "inventory_stockalert": {"created_at", "updated_at"},
            "inventory_supplier": {"contact_name"},
        }

        for table_name, expected_columns in required_columns.items():
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                """,
                [schema, table_name],
            )
            columns = {row[0] for row in cursor.fetchall()}
            if not expected_columns.issubset(columns):
                return False

        return True

    def _schema_matches_network_0014(self, cursor, schema: str) -> bool:
        if not self._table_has_columns(
            cursor,
            schema,
            "network_router",
            {
                "api_remote_port",
                "winbox_remote_port",
                "wireguard_private_key",
                "wireguard_public_key",
            },
        ):
            return False

        return self._table_has_indexes(
            cursor,
            schema,
            "network_router",
            {
                "network_rou_winbox__43f123_idx",
                "network_rou_api_rem_1c08dd_idx",
            },
        )

    def _schema_matches_core_0014(self, cursor, schema: str) -> bool:
        return self._table_has_columns(
            cursor,
            schema,
            "core_lead",
            {
                "is_contacted",
                "contacted_at",
            },
        )

    def _table_has_columns(self, cursor, schema: str, table_name: str, expected_columns: set[str]) -> bool:
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            """,
            [schema, table_name],
        )
        columns = {row[0] for row in cursor.fetchall()}
        return expected_columns.issubset(columns)

    def _table_has_indexes(self, cursor, schema: str, table_name: str, expected_indexes: set[str]) -> bool:
        cursor.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = %s
              AND tablename = %s
            """,
            [schema, table_name],
        )
        indexes = {row[0] for row in cursor.fetchall()}
        return expected_indexes.issubset(indexes)
