"""
One-time repair command: fix token_blacklist FK constraints in all tenant schemas.

Background
----------
The `token_blacklist` app was historically placed in SHARED_APPS which caused its
migrations to run against the public schema only.  The tenant schemas had these
migrations recorded as "applied" in their django_migrations table (because
django-tenants ran them) but the resilient migration command skipped the actual
table creation (it saw the public schema table via search_path and silently
skipped the CREATE TABLE).

Result: all tenant schemas have django_migrations entries for token_blacklist.*
but NO actual tables — so every login attempt raises:
  IntegrityError: insert or update on table "token_blacklist_outstandingtoken"
  violates foreign key constraint ... Key (user_id)=(...) is not present in core_user.

What this command does (per tenant schema)
------------------------------------------
Case A — tables absent (most common after SHARED_APPS removal):
  1. Delete the stale token_blacklist.* rows from the tenant's django_migrations
     so they are treated as unapplied.
  2. Create the tables directly with schema-correct SQL (FK → tenant's core_user).
  3. Re-insert the migration rows as applied.

Case B — tables present but FK points to public.core_user:
  1. Delete orphaned OutstandingToken rows whose user_id is absent from
     the tenant's own core_user.
  2. Drop the mis-wired FK constraint.
  3. Re-add it so it references the current (tenant) schema's core_user.

Case C — tables present, FK correct:
  Nothing to do.

Run:
    python manage.py fix_tenant_token_blacklist
    python manage.py fix_tenant_token_blacklist --dry-run   # preview only
    python manage.py fix_tenant_token_blacklist --schema tenant_abc
"""

import logging

from django.core.management.base import BaseCommand
from django.db import connection, transaction

logger = logging.getLogger(__name__)

CONSTRAINT_NAME = "token_blacklist_outs_user_id_83bc629a_fk_core_user"
OUTSTANDING_TABLE = "token_blacklist_outstandingtoken"
BLACKLISTED_TABLE = "token_blacklist_blacklistedtoken"
CORE_USER_TABLE = "core_user"

# All migration names that belong to the token_blacklist app.
# These are cleared from the tenant's django_migrations when tables are absent
# so the migrations can be re-applied (creating tables with correct FKs).
TOKEN_BLACKLIST_MIGRATIONS = [
    "0001_initial",
    "0002_outstandingtoken_replace_jti_with_uuid_jti_field",
    "0003_auto_20171017_2007",
    "0004_auto_20190301_0930",
    "0005_remove_outstandingtoken_jti",
    "0006_auto_20190406_1805",
    "0007_auto_20200801_0054",
    "0008_migrate_to_bigautofield",
    "0009_add_created_at_field",
    "0010_fix_migrate_to_bigautofield",
    "0011_alter_outstandingtoken_user",
    "0012_alter_outstandingtoken_user",
]


def _table_exists_in_schema(cursor, schema, table):
    """Return True if *table* exists in *schema* (checks information_schema directly)."""
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        LIMIT 1
        """,
        [schema, table],
    )
    return cursor.fetchone() is not None


def _fk_references_schema(cursor, schema, constraint_name):
    """
    Return the schema the FK *constraint_name* in *schema* references.
    Returns None if the constraint doesn't exist.
    """
    cursor.execute(
        """
        SELECT n_ref.nspname
        FROM pg_catalog.pg_constraint c
        JOIN pg_catalog.pg_class cl ON cl.oid = c.conrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = cl.relnamespace
        JOIN pg_catalog.pg_class cl_ref ON cl_ref.oid = c.confrelid
        JOIN pg_catalog.pg_namespace n_ref ON n_ref.oid = cl_ref.relnamespace
        WHERE n.nspname = %s AND c.conname = %s AND c.contype = 'f'
        LIMIT 1
        """,
        [schema, constraint_name],
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _clear_migration_records(cursor, schema):
    """Delete all token_blacklist migration records from the tenant's django_migrations."""
    cursor.execute(
        f"""
        DELETE FROM "{schema}".django_migrations
        WHERE app = 'token_blacklist'
        """
    )
    return cursor.rowcount


def _create_token_blacklist_tables(cursor, schema):
    """
    Create token_blacklist tables directly in the tenant schema with the FK
    referencing THAT schema's core_user (not the public one).

    This avoids relying on migrate_schemas which may skip because migrations
    are already recorded as applied.  We use schema-qualified names for safety.
    """
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{schema}".{OUTSTANDING_TABLE} (
            id         bigserial    NOT NULL PRIMARY KEY,
            token      text         NOT NULL,
            created_at timestamptz  NULL,
            expires_at timestamptz  NOT NULL,
            user_id    bigint       NULL
                       REFERENCES "{schema}".{CORE_USER_TABLE}(id)
                       DEFERRABLE INITIALLY DEFERRED,
            jti        varchar(255) NOT NULL
        )
        """
    )
    # Unique index on jti
    cursor.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS
            token_blacklist_outstandingtoken_jti_hex_d9bdf6f7_uniq
        ON "{schema}".{OUTSTANDING_TABLE} (jti)
        """
    )
    # Index on user_id
    cursor.execute(
        f"""
        CREATE INDEX IF NOT EXISTS
            token_blacklist_outstandingtoken_user_id_83bc629a
        ON "{schema}".{OUTSTANDING_TABLE} (user_id)
        """
    )
    # BlacklistedToken table
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS "{schema}".{BLACKLISTED_TABLE} (
            id              bigserial   NOT NULL PRIMARY KEY,
            blacklisted_at  timestamptz NOT NULL,
            token_id        bigint      NOT NULL UNIQUE
                            REFERENCES "{schema}".{OUTSTANDING_TABLE}(id)
        )
        """
    )
    # Index on token_id
    cursor.execute(
        f"""
        CREATE INDEX IF NOT EXISTS
            token_blacklist_blacklistedtoken_token_id_d3aa49ae
        ON "{schema}".{BLACKLISTED_TABLE} (token_id)
        """
    )


def _record_migrations_applied(cursor, schema):
    """Insert all token_blacklist migrations as applied in the tenant's django_migrations."""
    import django.utils.timezone as tz
    now = tz.now()
    for migration_name in TOKEN_BLACKLIST_MIGRATIONS:
        cursor.execute(
            f"""
            INSERT INTO "{schema}".django_migrations (app, name, applied)
            VALUES ('token_blacklist', %s, %s)
            ON CONFLICT (app, name) DO NOTHING
            """,
            [migration_name, now],
        )


def _fix_schema(cursor, schema, dry_run, stdout, style):
    """Repair token_blacklist for one tenant *schema*. Returns status string."""

    core_user_exists = _table_exists_in_schema(cursor, schema, CORE_USER_TABLE)
    if not core_user_exists:
        stdout.write(style.WARNING(
            f"  [{schema}] SKIP — no core_user table (not a tenant schema?)"
        ))
        return "skipped"

    outstanding_exists = _table_exists_in_schema(cursor, schema, OUTSTANDING_TABLE)

    # ── Case A: tables missing ────────────────────────────────────────────────
    if not outstanding_exists:
        stdout.write(style.WARNING(
            f"  [{schema}] Tables absent — creating with correct FK → {schema}.core_user"
        ))
        if dry_run:
            return "would_fix"

        # 1. Clear stale migration records so we own the state cleanly.
        cleared = _clear_migration_records(cursor, schema)
        if cleared:
            stdout.write(f"    Cleared {cleared} stale token_blacklist migration record(s)")

        # 2. Create tables with FK pointing to THIS schema's core_user.
        _create_token_blacklist_tables(cursor, schema)

        # 3. Mark all migrations as applied.
        _record_migrations_applied(cursor, schema)

        stdout.write(style.SUCCESS(
            f"  [{schema}] Created — tables now exist with correct FK"
        ))
        return "created"

    # ── Case B: tables exist — check FK ──────────────────────────────────────
    referenced_schema = _fk_references_schema(cursor, schema, CONSTRAINT_NAME)

    if referenced_schema is None:
        stdout.write(style.WARNING(
            f"  [{schema}] FK constraint missing — adding → {schema}.core_user"
        ))
        if not dry_run:
            cursor.execute(
                f"""
                ALTER TABLE "{schema}".{OUTSTANDING_TABLE}
                ADD CONSTRAINT {CONSTRAINT_NAME}
                FOREIGN KEY (user_id)
                REFERENCES "{schema}".{CORE_USER_TABLE}(id)
                DEFERRABLE INITIALLY DEFERRED
                """
            )
        return "would_fix" if dry_run else "fk_added"

    if referenced_schema == schema:
        stdout.write(f"  [{schema}] OK — FK already references {schema}.core_user")
        return "ok"

    # ── Case C: FK points to wrong schema ────────────────────────────────────
    stdout.write(style.WARNING(
        f"  [{schema}] BAD FK → {referenced_schema}.core_user (fixing to {schema}.core_user)"
    ))
    if dry_run:
        return "would_fix"

    # Remove orphaned rows first to satisfy the new FK.
    cursor.execute(
        f"""
        DELETE FROM "{schema}".{OUTSTANDING_TABLE}
        WHERE user_id NOT IN (SELECT id FROM "{schema}".{CORE_USER_TABLE})
        """
    )
    deleted = cursor.statusmessage
    if deleted and deleted != "DELETE 0":
        stdout.write(style.WARNING(f"    Removed orphaned rows: {deleted}"))

    cursor.execute(
        f"""
        ALTER TABLE "{schema}".{OUTSTANDING_TABLE}
        DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME}
        """
    )
    cursor.execute(
        f"""
        ALTER TABLE "{schema}".{OUTSTANDING_TABLE}
        ADD CONSTRAINT {CONSTRAINT_NAME}
        FOREIGN KEY (user_id)
        REFERENCES "{schema}".{CORE_USER_TABLE}(id)
        DEFERRABLE INITIALLY DEFERRED
        """
    )
    stdout.write(style.SUCCESS(f"  [{schema}] Fixed — FK now → {schema}.core_user"))
    return "fixed"


class Command(BaseCommand):
    help = (
        "Repair token_blacklist tables in every tenant schema: "
        "create missing tables with the correct FK → tenant core_user."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report issues without making any database changes.",
        )
        parser.add_argument(
            "--schema",
            type=str,
            help="Repair a single named schema instead of all tenants.",
        )

    def handle(self, *args, **options):
        from django_tenants.utils import get_public_schema_name, schema_context
        from apps.core.models import Tenant

        dry_run = options["dry_run"]
        target_schema = options.get("schema")

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes will be made\n"))

        with schema_context(get_public_schema_name()):
            qs = Tenant.objects.exclude(schema_name=get_public_schema_name())
            if target_schema:
                qs = qs.filter(schema_name=target_schema)
            schemas = list(qs.values_list("schema_name", flat=True))

        if not schemas:
            self.stdout.write(self.style.WARNING("No tenant schemas found."))
            return

        self.stdout.write(f"Checking {len(schemas)} tenant schema(s)...\n")

        counts: dict[str, int] = {}

        with connection.cursor() as cursor:
            for schema in schemas:
                try:
                    with transaction.atomic():
                        status = _fix_schema(cursor, schema, dry_run, self.stdout, self.style)
                        counts[status] = counts.get(status, 0) + 1
                        if dry_run and status == "would_fix":
                            raise Exception("dry-run rollback")
                except Exception as exc:
                    if dry_run and "dry-run rollback" in str(exc):
                        continue
                    self.stdout.write(self.style.ERROR(f"  [{schema}] ERROR: {exc}"))
                    logger.exception("fix_tenant_token_blacklist failed for %s", schema)
                    counts["error"] = counts.get("error", 0) + 1

        # Reset search_path
        with connection.cursor() as cursor:
            cursor.execute(f"SET search_path TO {get_public_schema_name()}, public")

        self.stdout.write("")
        self.stdout.write("─" * 60)
        self.stdout.write(f"Results for {len(schemas)} schema(s):")
        self.stdout.write(self.style.SUCCESS(
            f"  OK (already correct)  : {counts.get('ok', 0)}"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"  Created (tables built): {counts.get('created', 0)}"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"  Fixed (FK corrected)  : {counts.get('fixed', 0)}"
        ))
        self.stdout.write(self.style.WARNING(
            f"  FK added (was missing): {counts.get('fk_added', 0)}"
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"  Would fix (dry-run)   : {counts.get('would_fix', 0)}"
            ))
        self.stdout.write(self.style.WARNING(
            f"  Skipped               : {counts.get('skipped', 0)}"
        ))
        if counts.get("error"):
            self.stdout.write(self.style.ERROR(
                f"  Errors                : {counts.get('error', 0)}"
            ))
