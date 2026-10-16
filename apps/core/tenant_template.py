"""
Template-schema cloning for fast tenant provisioning.

template_tenant is a bare PostgreSQL schema (NO Tenant row), so every
`Tenant.objects.exclude(schema_name='public')` loop (Celery, middleware,
purge) ignores it automatically.
"""
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django_tenants.clone import CloneSchema
from django_tenants.utils import schema_context, schema_exists

logger = logging.getLogger(__name__)

TEMPLATE_SCHEMA = "template_tenant"  # cannot collide: real tenants are "tenant_<slug>"
_LOCK_KEY = "netily:template_tenant"


@contextmanager
def template_lock(exclusive: bool):
    """Clones take a shared lock, template refresh takes an exclusive one."""
    lock = "pg_advisory_lock" if exclusive else "pg_advisory_lock_shared"
    unlock = "pg_advisory_unlock" if exclusive else "pg_advisory_unlock_shared"
    with connection.cursor() as cur:
        cur.execute(f"SELECT {lock}(hashtext(%s))", [_LOCK_KEY])
    try:
        yield
    finally:
        with connection.cursor() as cur:
            cur.execute(f"SELECT {unlock}(hashtext(%s))", [_LOCK_KEY])


def _tenant_app_labels() -> set:
    tenant_apps = set(settings.TENANT_APPS)
    return {c.label for c in apps.get_app_configs() if c.name in tenant_apps}


def pending_migrations() -> list:
    """Migrations in code that the template has not applied yet."""
    labels = _tenant_app_labels()
    with schema_context(TEMPLATE_SCHEMA):
        executor = MigrationExecutor(connection)
        targets = [n for n in executor.loader.graph.leaf_nodes() if n[0] in labels]
        return executor.migration_plan(targets)


def _create_schema(schema_name: str) -> None:
    with connection.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"')


def _drop_schema(schema_name: str) -> None:
    with connection.cursor() as cur:
        cur.execute('SET search_path TO "public"')
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')


def _migrate_schema_subprocess(schema_name: str) -> None:
    """Isolated process, same behaviour as the old registration flow."""
    env = os.environ.copy()
    env.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
    result = subprocess.run(
        [sys.executable, "manage.py", "migrate_schemas_resilient", "--schema", schema_name],
        cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, timeout=240,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Migration failed for {schema_name}\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )


def refresh_template(rebuild: bool = False) -> dict:
    """Create the template, or apply only the pending migrations to it."""
    t0 = time.perf_counter()
    with template_lock(exclusive=True):
        if rebuild and schema_exists(TEMPLATE_SCHEMA):
            _drop_schema(TEMPLATE_SCHEMA)
        _create_schema(TEMPLATE_SCHEMA)
        call_command(
            "migrate_schemas", "--tenant", f"--schema={TEMPLATE_SCHEMA}",
            interactive=False, verbosity=1,
        )
    connection.set_schema_to_public()
    return {"seconds": round(time.perf_counter() - t0, 1), "pending_after": len(pending_migrations())}


def provision_tenant_schema(schema_name: str) -> dict:
    """
    Clone template -> schema_name. Self-healing:
      * template missing         -> old path (create + full migrate)
      * template behind the code -> clone, then migrate ONLY the missing ones
    """
    t0 = time.perf_counter()

    if not schema_exists(TEMPLATE_SCHEMA):
        logger.warning("template_tenant missing, falling back to full migrate for %s", schema_name)
        _create_schema(schema_name)
        try:
            _migrate_schema_subprocess(schema_name)
        except Exception:
            _drop_schema(schema_name)
            raise
        return {"mode": "full_migrate", "seconds": round(time.perf_counter() - t0, 1)}

    pending = pending_migrations()
    try:
        with template_lock(exclusive=False):
            CloneSchema().clone_schema(TEMPLATE_SCHEMA, schema_name, set_connection=False)
        connection.set_schema_to_public()
        if pending:
            logger.warning("Template behind by %d migration(s); catching up %s", len(pending), schema_name)
            _migrate_schema_subprocess(schema_name)
    except Exception:
        connection.set_schema_to_public()
        _drop_schema(schema_name)
        raise

    return {"mode": "clone", "caught_up": len(pending), "seconds": round(time.perf_counter() - t0, 1)}