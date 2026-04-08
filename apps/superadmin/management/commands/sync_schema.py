"""
One-shot command: ensure every column/table defined in Django models
actually exists in the database.

After fake_applied_migrations faked too aggressively, some migrations'
DB changes don't actually exist (e.g. subscriptions.pppoe_min_clients
on public schema).  This command introspects every model vs every
schema and fills the gaps using SchemaEditor.

SAFE for live data — only ADDs missing tables/columns, never drops anything.
"""
import logging
from django.apps import apps as django_apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Add missing tables and columns to match Django model definitions"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Preview what would be created without touching the DB',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        if dry_run:
            self.stdout.write(self.style.WARNING("══════ DRY RUN ══════\n"))

        shared = {a.split('.')[-1] for a in settings.SHARED_APPS if a.startswith('apps.')}
        tenant = {a.split('.')[-1] for a in settings.TENANT_APPS if a.startswith('apps.')}

        schemas = self._get_schemas()
        total = 0

        for schema in schemas:
            is_public = schema == 'public'
            labels = shared if is_public else tenant
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n── {schema} ──"))
            total += self._sync(schema, labels, dry_run)

        verb = "Would add" if dry_run else "Added"
        self.stdout.write(self.style.SUCCESS(
            f"\n══════ DONE — {verb} {total} object(s) ══════"
        ))

    # ── internals ────────────────────────────────────────────

    def _get_schemas(self):
        schemas = ['public']
        with connection.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name LIKE 'tenant_%%'"
            )
            schemas.extend(row[0] for row in cur.fetchall())
        return schemas

    def _sync(self, schema, labels, dry_run):
        fixes = 0
        with connection.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')

        for label in sorted(labels):
            try:
                app = django_apps.get_app_config(label)
            except LookupError:
                continue

            for model in app.get_models():
                fixes += self._sync_model(schema, model, dry_run)
        return fixes

    def _sync_model(self, schema, model, dry_run):
        fixes = 0
        table = model._meta.db_table

        with connection.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s)",
                [schema, table],
            )
            table_exists = cur.fetchone()[0]

        if not table_exists:
            self.stdout.write(f"  TABLE MISSING: {table}")
            if not dry_run:
                try:
                    with connection.cursor() as cur:
                        cur.execute(f'SET search_path TO "{schema}"')
                    with connection.schema_editor() as editor:
                        editor.create_model(model)
                    self.stdout.write(self.style.SUCCESS(f"    -> created"))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"    -> ERROR: {e}"))
            return 1  # count the whole table as one fix

        # ── table exists — check columns ──
        with connection.cursor() as cur:
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                [schema, table],
            )
            existing = {row[0] for row in cur.fetchall()}

        for field in model._meta.local_concrete_fields:
            col = field.column
            if col in existing:
                continue

            self.stdout.write(f"  COLUMN MISSING: {table}.{col}")
            fixes += 1
            if not dry_run:
                try:
                    with connection.cursor() as cur:
                        cur.execute(f'SET search_path TO "{schema}"')
                    with connection.schema_editor() as editor:
                        editor.add_field(model, field)
                    self.stdout.write(self.style.SUCCESS(f"    -> added"))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"    -> ERROR: {e}"))

        # ── auto-created M2M intermediate tables ──
        for field in model._meta.local_many_to_many:
            through = field.remote_field.through
            if not through._meta.auto_created:
                continue
            m2m_table = field.m2m_db_table()
            with connection.cursor() as cur:
                cur.execute(f'SET search_path TO "{schema}"')
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = %s)",
                    [schema, m2m_table],
                )
                if not cur.fetchone()[0]:
                    self.stdout.write(f"  M2M TABLE MISSING: {m2m_table}")
                    fixes += 1
                    if not dry_run:
                        try:
                            with connection.schema_editor() as editor:
                                editor.create_model(through)
                            self.stdout.write(self.style.SUCCESS(f"    -> created"))
                        except Exception as e:
                            self.stdout.write(self.style.ERROR(f"    -> ERROR: {e}"))
        return fixes
