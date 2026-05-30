"""
Management command to repair inconsistent migration history.

When migration files are added to git after being previously ignored,
Django detects that some migrations were applied without their dependencies
being recorded. This command finds those gaps and fake-applies them.

It ONLY fakes migrations whose dependents are ALREADY recorded in
django_migrations, ensuring genuinely new migrations are left to run normally.

Usage:
    # Preview what would be faked on public schema
    python manage.py repair_migrations --dry-run

    # Fix public schema
    python manage.py repair_migrations

    # Fix a tenant schema
    python manage.py repair_migrations --schema tenant_green

    # Fix all tenant schemas + public
    python manage.py repair_migrations --all-schemas
"""
import logging
from django.core.management.base import BaseCommand
from django.db import connection

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Repair inconsistent migration history caused by missing historical migration files"

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema",
            type=str,
            default="public",
            help="PostgreSQL schema to repair (default: public)",
        )
        parser.add_argument(
            "--all-schemas",
            action="store_true",
            help="Repair all schemas (public + all tenant schemas)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be faked without making changes",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        all_schemas = options["all_schemas"]
        schema = options["schema"]

        if all_schemas:
            schemas = sorted(self._get_existing_schemas())
        else:
            schemas = [schema]

        for s in schemas:
            self._repair_schema(s, dry_run)

    def _get_existing_schemas(self):
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT schema_name
                FROM information_schema.schemata
                WHERE schema_name NOT IN ('information_schema', 'pg_catalog', 'pg_toast')
                ORDER BY schema_name
                """
            )
            return [row[0] for row in cur.fetchall()]

    def _repair_schema(self, schema, dry_run):
        from django.apps import apps as django_apps
        import importlib, os

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== Repairing schema: {schema} ==="))

        # -- 1. Set the correct search_path so queries go to the right schema --
        with connection.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')

        with connection.cursor() as cur:
            cur.execute(
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
            if not cur.fetchone()[0]:
                self.stdout.write(
                    self.style.WARNING(f"  No django_migrations table in schema '{schema}'. Skipping.")
                )
                return

        # -- 2. Load applied migrations DIRECTLY from the DB (bypasses consistency check) --
        with connection.cursor() as cur:
            cur.execute("SELECT app, name FROM django_migrations")
            applied = {(app, name) for app, name in cur.fetchall()}

        if not applied:
            self.stdout.write(self.style.WARNING(f"  No migrations recorded in schema '{schema}'. Skipping."))
            return

        self.stdout.write(f"  {len(applied)} migrations currently recorded in {schema}")

        # -- 3. Read migration files directly from disk (no MigrationLoader = no consistency check) --
        disk_migrations = {}  # (app_label, name) -> migration module
        for app_config in django_apps.get_app_configs():
            try:
                migrations_module_name = f"{app_config.name}.migrations"
                migrations_module = importlib.import_module(migrations_module_name)
                migrations_path = os.path.dirname(migrations_module.__file__)
                for fname in sorted(os.listdir(migrations_path)):
                    if fname.endswith(".py") and not fname.startswith("_"):
                        name = fname[:-3]
                        try:
                            mod = importlib.import_module(f"{migrations_module_name}.{name}")
                            disk_migrations[(app_config.label, name)] = mod
                        except Exception:
                            pass
            except (ImportError, AttributeError, TypeError):
                pass

        self.stdout.write(f"  {len(disk_migrations)} migration files found on disk")

        # -- 4. Find migrations that SHOULD be faked --
        # A migration should be faked if:
        #   a) It exists on disk
        #   b) It is NOT recorded in the DB (not applied)
        #   c) At least one migration that DEPENDS ON IT is recorded in the DB
        #
        # This means it was implicitly applied (table already exists) but not tracked.

        def get_dependencies(migration_module):
            """Extract dependencies from a migration file's Migration class."""
            migration_cls = getattr(migration_module, "Migration", None)
            if migration_cls is None:
                return []
            return getattr(migration_cls, "dependencies", [])

        # Build reverse dependency map: who depends on (app, name)?
        dependents = {}  # (app, name) -> list of (app, name) that depend on it
        for (app, name), mod in disk_migrations.items():
            for dep_app, dep_name in get_dependencies(mod):
                key = (dep_app, dep_name)
                dependents.setdefault(key, []).append((app, name))

        # Iteratively find missing deps: a migration needs faking if any applied migration
        # (directly or indirectly) depends on it and it isn't applied.
        to_fake = set()
        changed = True
        while changed:
            changed = False
            for (app, name), mod in disk_migrations.items():
                if (app, name) in applied or (app, name) in to_fake:
                    continue
                # Check if any APPLIED migration directly depends on this one
                for dep_app, dep_name in get_dependencies(mod):
                    # This migration (app, name) is a dependency of (dep_app, dep_name)
                    # but we need to check the other way: who has THIS as a dependency?
                    pass

            # Correct approach: for each applied or to_fake migration, check its deps
            for (app, name) in list(applied) + list(to_fake):
                mod = disk_migrations.get((app, name))
                if mod is None:
                    continue
                for dep_app, dep_name in get_dependencies(mod):
                    if (dep_app, dep_name) not in applied and (dep_app, dep_name) not in to_fake:
                        if (dep_app, dep_name) in disk_migrations:
                            to_fake.add((dep_app, dep_name))
                            changed = True

        if not to_fake:
            self.stdout.write(self.style.SUCCESS(f"  No inconsistencies found in schema '{schema}'. All good!"))
            return

        # Sort by app label then migration name for deterministic output
        sorted_to_fake = sorted(to_fake)
        self.stdout.write(
            self.style.WARNING(f"  Found {len(sorted_to_fake)} migration(s) to fake:")
        )

        for app, name in sorted_to_fake:
            if dry_run:
                self.stdout.write(f"    DRY RUN would fake: {app}.{name}")
            else:
                with connection.cursor() as cur:
                    cur.execute(
                        "INSERT INTO django_migrations (app, name, applied) VALUES (%s, %s, NOW()) "
                        "ON CONFLICT DO NOTHING",
                        [app, name],
                    )
                self.stdout.write(self.style.SUCCESS(f"    Faked: {app}.{name}"))

        if not dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\n  Repaired {len(sorted_to_fake)} migration record(s) in schema '{schema}'.\n"
                    f"  Now run: python manage.py migrate_schemas --shared   (or --tenant)"
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  DRY RUN complete. Re-run without --dry-run to apply."
                )
            )
