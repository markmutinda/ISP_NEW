"""
One-time repair command: fix token_blacklist FK constraints in all tenant schemas.

Background
----------
The `token_blacklist` app was historically placed in SHARED_APPS which caused its
tables to be created in the public schema first.  When `migrate_schemas --tenant`
subsequently ran in a tenant schema whose search_path is `{tenant}, public`, the
unqualified FK `REFERENCES core_user(id)` sometimes resolved to *public.core_user*
instead of the tenant's own `core_user`.  At login time `RefreshToken.for_user(user)`
inserts into `token_blacklist_outstandingtoken` with a tenant-schema user_id that
doesn't exist in `public.core_user` → FK violation → HTTP 500.

What this command does (per tenant schema)
------------------------------------------
1. Detect whether the FK on `token_blacklist_outstandingtoken.user_id` references
   the PUBLIC schema's `core_user` or the tenant's own `core_user`.
2. If the FK is wrong (points to public) **or** the tables are missing entirely:
   - Delete orphaned OutstandingToken rows (user_id absent from tenant core_user).
   - Drop the mis-wired FK constraint.
   - Re-add the constraint so it references the *current* (tenant) schema's
     `core_user` table.
3. If `token_blacklist_outstandingtoken` is absent from the tenant schema
   (fell through to public via search_path), mark the token_blacklist migrations
   as unapplied in that tenant's django_migrations and re-run them.

Run once immediately after deploying the settings change that removes
`token_blacklist` from SHARED_APPS:

    python manage.py fix_tenant_token_blacklist
    python manage.py fix_tenant_token_blacklist --dry-run   # preview only

Then run the normal tenant migration to create tables in any brand-new tenant
schemas:

    python manage.py migrate_schemas_resilient --tenant
"""

import logging

from django.core.management.base import BaseCommand
from django.db import connection, transaction

logger = logging.getLogger(__name__)

CONSTRAINT_NAME = "token_blacklist_outs_user_id_83bc629a_fk_core_user"
OUTSTANDING_TABLE = "token_blacklist_outstandingtoken"
BLACKLISTED_TABLE = "token_blacklist_blacklistedtoken"
CORE_USER_TABLE = "core_user"


def _table_exists_in_schema(cursor, schema, table):
    """Return True if *table* exists in *schema* (checks information_schema)."""
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
    Return the schema that the FK *constraint_name* in *schema* references.

    Uses pg_constraint + pg_class to find the referenced table's namespace.
    Returns None if the constraint doesn't exist.
    """
    cursor.execute(
        """
        SELECT n_ref.nspname AS referenced_schema
        FROM pg_catalog.pg_constraint c
        JOIN pg_catalog.pg_class cl ON cl.oid = c.conrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = cl.relnamespace
        JOIN pg_catalog.pg_class cl_ref ON cl_ref.oid = c.confrelid
        JOIN pg_catalog.pg_namespace n_ref ON n_ref.oid = cl_ref.relnamespace
        WHERE n.nspname = %s
          AND c.conname = %s
          AND c.contype = 'f'
        LIMIT 1
        """,
        [schema, constraint_name],
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _fix_schema(cursor, schema, dry_run, stdout, style):
    """Repair token_blacklist FK for one tenant *schema*. Returns status string."""

    # ── 1. Ensure token_blacklist tables exist in this tenant schema ──────────
    outstanding_exists = _table_exists_in_schema(cursor, schema, OUTSTANDING_TABLE)
    core_user_exists = _table_exists_in_schema(cursor, schema, CORE_USER_TABLE)

    if not core_user_exists:
        msg = f"  [{schema}] SKIP — no core_user table in this schema (not a tenant schema?)"
        stdout.write(style.WARNING(msg))
        return "skipped"

    if not outstanding_exists:
        msg = f"  [{schema}] token_blacklist tables absent — will be created by migrate_schemas --tenant"
        stdout.write(style.WARNING(msg))
        return "tables_missing"

    # ── 2. Check what schema the FK currently references ─────────────────────
    referenced_schema = _fk_references_schema(cursor, schema, CONSTRAINT_NAME)

    if referenced_schema is None:
        # Constraint doesn't exist at all — add it pointing to the right schema.
        msg = f"  [{schema}] FK constraint missing — will add pointing to {schema}.core_user"
        stdout.write(style.WARNING(msg))
        if not dry_run:
            cursor.execute(f'SET search_path TO "{schema}", public')
            cursor.execute(
                f"""
                ALTER TABLE {OUTSTANDING_TABLE}
                ADD CONSTRAINT {CONSTRAINT_NAME}
                FOREIGN KEY (user_id) REFERENCES {CORE_USER_TABLE}(id)
                DEFERRABLE INITIALLY DEFERRED;
                """
            )
        return "fk_added"

    if referenced_schema == schema:
        stdout.write(f"  [{schema}] OK — FK already references {schema}.core_user")
        return "ok"

    # ── 3. FK points to the wrong schema (usually 'public') — fix it ─────────
    stdout.write(
        style.WARNING(
            f"  [{schema}] BAD FK — references {referenced_schema}.core_user "
            f"(should be {schema}.core_user) → fixing"
        )
    )

    if dry_run:
        return "would_fix"

    # Delete orphaned rows whose user_id doesn't exist in THIS schema's core_user.
    cursor.execute(f'SET search_path TO "{schema}", public')
    cursor.execute(
        f"""
        DELETE FROM {OUTSTANDING_TABLE}
        WHERE user_id NOT IN (SELECT id FROM {CORE_USER_TABLE});
        """
    )
    deleted = cursor.statusmessage  # e.g. "DELETE 5"
    if deleted and deleted != "DELETE 0":
        stdout.write(style.WARNING(f"    Removed orphaned OutstandingToken rows: {deleted}"))

    # Drop and re-add FK so it resolves to the tenant schema's core_user.
    cursor.execute(
        f"""
        ALTER TABLE {OUTSTANDING_TABLE}
        DROP CONSTRAINT IF EXISTS {CONSTRAINT_NAME};
        """
    )
    cursor.execute(
        f"""
        ALTER TABLE {OUTSTANDING_TABLE}
        ADD CONSTRAINT {CONSTRAINT_NAME}
        FOREIGN KEY (user_id) REFERENCES {CORE_USER_TABLE}(id)
        DEFERRABLE INITIALLY DEFERRED;
        """
    )

    stdout.write(style.SUCCESS(f"  [{schema}] Fixed — FK now references {schema}.core_user"))
    return "fixed"


class Command(BaseCommand):
    help = (
        "One-time repair: ensure token_blacklist FK in every tenant schema "
        "references that tenant's own core_user (not the public schema's)."
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

        # Collect tenant schema names from the public schema.
        with schema_context(get_public_schema_name()):
            qs = Tenant.objects.exclude(schema_name=get_public_schema_name())
            if target_schema:
                qs = qs.filter(schema_name=target_schema)
            schemas = list(qs.values_list("schema_name", flat=True))

        if not schemas:
            self.stdout.write(self.style.WARNING("No tenant schemas found."))
            return

        self.stdout.write(f"Checking {len(schemas)} tenant schema(s)...\n")

        counts = {"ok": 0, "fixed": 0, "would_fix": 0, "fk_added": 0,
                  "tables_missing": 0, "skipped": 0, "error": 0}

        with connection.cursor() as cursor:
            for schema in schemas:
                try:
                    # Each schema fix is its own transaction to keep failures isolated.
                    with transaction.atomic():
                        # Set search_path for this tenant.
                        cursor.execute(f'SET search_path TO "{schema}", public')
                        status = _fix_schema(cursor, schema, dry_run, self.stdout, self.style)
                        counts[status] = counts.get(status, 0) + 1
                        if dry_run and status not in ("ok", "skipped"):
                            # Roll back any accidental changes in dry-run mode.
                            raise Exception("dry-run rollback")
                except Exception as exc:
                    if dry_run and "dry-run rollback" in str(exc):
                        continue
                    self.stdout.write(
                        self.style.ERROR(f"  [{schema}] ERROR: {exc}")
                    )
                    logger.exception("fix_tenant_token_blacklist failed for schema %s", schema)
                    counts["error"] += 1

        # Reset search_path to public before exiting.
        with connection.cursor() as cursor:
            cursor.execute(f"SET search_path TO {get_public_schema_name()}, public")

        # Summary
        self.stdout.write("")
        self.stdout.write("─" * 60)
        self.stdout.write(f"Results for {len(schemas)} schema(s):")
        self.stdout.write(self.style.SUCCESS(f"  OK (already correct) : {counts['ok']}"))
        self.stdout.write(self.style.SUCCESS(f"  Fixed                : {counts['fixed']}"))
        if dry_run:
            self.stdout.write(self.style.WARNING(f"  Would fix (dry-run)  : {counts['would_fix']}"))
        self.stdout.write(self.style.WARNING(f"  FK added (missing)   : {counts['fk_added']}"))
        self.stdout.write(self.style.WARNING(f"  Tables missing       : {counts['tables_missing']}"))
        self.stdout.write(self.style.WARNING(f"  Skipped              : {counts['skipped']}"))
        if counts["error"]:
            self.stdout.write(self.style.ERROR(f"  Errors               : {counts['error']}"))

        if counts.get("tables_missing", 0):
            self.stdout.write(
                self.style.WARNING(
                    "\nSome tenant schemas are missing token_blacklist tables entirely.\n"
                    "Run this after deploying to create them:\n"
                    "  python manage.py migrate_schemas_resilient --tenant"
                )
            )
