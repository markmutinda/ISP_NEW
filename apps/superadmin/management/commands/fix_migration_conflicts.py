"""
One-shot management command to fix migration conflicts caused by two parallel
branches of migration files coexisting after a gitignore removal.

Strategy:
1. Delete the orphaned 'tenant_blue' (no schema exists)
2. Delete Branch B migration files (server-generated, duplicate work)
3. Delete all merge migrations (no longer needed)
4. Fake the Branch A migrations that aren't yet recorded but whose changes
   already exist in the DB (because Branch B applied equivalent changes)
5. After this command, only genuinely new migrations remain to be applied

SAFE for live tenants: NO data is touched, NO tables are altered.
Only django_migrations records and .py files are affected.
"""
import os
import logging
from django.core.management.base import BaseCommand
from django.db import connection

logger = logging.getLogger(__name__)

# ── Files to DELETE (Branch B + merge files) ──────────────────────────
# These files duplicate work already done by Branch A and cause conflicts.
FILES_TO_DELETE = {
    'network': [
        '0002_routerconfiguration_and_more.py',
        '0003_router_vpn_certificate_router_vpn_ip_address_and_more.py',
        '0004_alter_router_api_password_and_more.py',
        '0013_merge_20260408_1910.py',
    ],
    'subscriptions': [
        '0002_netilyplan_base_license_fee_and_more.py',
        '0003_billingcycle_snapshot_base_fee_and_more.py',
        '0004_billingsnapshot.py',
        '0005_remove_billingcycle_snapshot_min_clients_and_more.py',
        '0006_merge_20260408_1910.py',
    ],
    'billing': [
        '0003_hotspotplan_plan_burst_download_plan_burst_enabled_and_more.py',
        '0004_mpesaconfiguration_mpesatransaction_and_more.py',
        '0005_alter_mpesatransaction_transaction_id.py',
        '0006_alter_mpesaconfiguration_schema_name.py',
        '0007_hotspotsession_is_roaming_hotspotsession_roamed_from.py',
        '0008_tenanttumaconfig_payment_tuma_callback_payload_and_more.py',
        '0009_alter_invoiceitempayment_channel_id_and_more.py',
        '0010_hotspotclient_hotspotclientdevice_and_more.py',
        '0011_voucherbatch_hotspot_plan_and_more.py',
        '0012_alter_voucher_schema_name_and_more.py',
        '0013_alter_payment_customer_alter_receipt_customer.py',
        '0014_alter_payment_customer_alter_receipt_customer.py',
        '0015_hotspotclient_canonical_username_and_more.py',
        '0016_merge_20260408_1910.py',
    ],
    'core': [
        '0002_globalroutermap.py',
        '0003_changelog.py',
        '0004_featurerequest_alter_changelog_release_date_and_more.py',
        '0005_routertenantindex.py',
        '0006_routertenantindex_router_id_and_more.py',
        '0007_tumacallbackmap.py',
        '0008_merge_20260408_1910.py',
    ],
    'customers': [
        '0002_alter_customer_id_number.py',
        '0004_merge_20260408_1910.py',
    ],
    'radius': [
        '0002_remove_radiustenantconfig_acct_port_and_more.py',
        '0003_customerradiuscredentials_subscription_activated_at.py',
        '0007_merge_20260408_1910.py',
    ],
}

# ── Branch B migration NAMES to purge from django_migrations ──────────
# If any of these got recorded, remove them so they don't confuse Django.
BRANCH_B_NAMES = {
    'network': [
        '0002_routerconfiguration_and_more',
        '0003_router_vpn_certificate_router_vpn_ip_address_and_more',
        '0004_alter_router_api_password_and_more',
        '0013_merge_20260408_1910',
    ],
    'subscriptions': [
        '0002_netilyplan_base_license_fee_and_more',
        '0003_billingcycle_snapshot_base_fee_and_more',
        '0004_billingsnapshot',
        '0005_remove_billingcycle_snapshot_min_clients_and_more',
        '0006_merge_20260408_1910',
    ],
    'billing': [
        '0003_hotspotplan_plan_burst_download_plan_burst_enabled_and_more',
        '0004_mpesaconfiguration_mpesatransaction_and_more',
        '0005_alter_mpesatransaction_transaction_id',
        '0006_alter_mpesaconfiguration_schema_name',
        '0007_hotspotsession_is_roaming_hotspotsession_roamed_from',
        '0008_tenanttumaconfig_payment_tuma_callback_payload_and_more',
        '0009_alter_invoiceitempayment_channel_id_and_more',
        '0010_hotspotclient_hotspotclientdevice_and_more',
        '0011_voucherbatch_hotspot_plan_and_more',
        '0012_alter_voucher_schema_name_and_more',
        '0013_alter_payment_customer_alter_receipt_customer',
        '0014_alter_payment_customer_alter_receipt_customer',
        '0015_hotspotclient_canonical_username_and_more',
        '0016_merge_20260408_1910',
    ],
    'core': [
        '0002_globalroutermap',
        '0003_changelog',
        '0004_featurerequest_alter_changelog_release_date_and_more',
        '0005_routertenantindex',
        '0006_routertenantindex_router_id_and_more',
        '0007_tumacallbackmap',
        '0008_merge_20260408_1910',
    ],
    'customers': [
        '0002_alter_customer_id_number',
        '0004_merge_20260408_1910',
    ],
    'radius': [
        '0002_remove_radiustenantconfig_acct_port_and_more',
        '0003_customerradiuscredentials_subscription_activated_at',
        '0007_merge_20260408_1910',
    ],
}

# ── Branch A migrations that need to be FAKED (DB already has the changes) ──
# These are from Branch A but the equivalent DB changes were made by Branch B.
# They need to be in django_migrations so Django doesn't try to re-apply them.
MIGRATIONS_TO_FAKE = {
    'network': [
        '0012_alter_ipaddress_ip_pool_alter_router_api_password_and_more',
    ],
    'billing': [
        '0008_hotspotclient_hotspotclientdevice_mpesaconfiguration_and_more',
    ],
    'analytics': [
        '0003_customerchurnevent',
    ],
}

# ── APP to migrations directory path mapping ──────────────────────────
APP_PATHS = {
    'network': 'apps/network/migrations',
    'subscriptions': 'apps/subscriptions/migrations',
    'billing': 'apps/billing/migrations',
    'core': 'apps/core/migrations',
    'customers': 'apps/customers/migrations',
    'radius': 'apps/radius/migrations',
}


class Command(BaseCommand):
    help = "Fix migration conflicts: delete duplicate files, purge stale records, fake applied-equivalent migrations"

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Preview changes without applying them')

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        if dry_run:
            self.stdout.write(self.style.WARNING("\n══════ DRY RUN MODE ══════\n"))

        # ─── Step 1: Delete orphaned tenant_blue ───
        self._delete_blue_tenant(dry_run)

        # ─── Step 2: Delete Branch B migration FILES ───
        self._delete_branch_b_files(dry_run)

        # ─── Step 3: Purge Branch B records from ALL schemas ───
        self._purge_branch_b_records(dry_run)

        # ─── Step 4: Fake Branch A migrations that are equivalent to applied Branch B ───
        self._fake_equivalent_migrations(dry_run)

        self.stdout.write(self.style.SUCCESS(
            "\n══════ DONE ══════\n"
            "Next steps:\n"
            "  1. python manage.py migrate_schemas --shared\n"
            "  2. python manage.py migrate_schemas --tenant\n"
        ))

    def _delete_blue_tenant(self, dry_run):
        """Delete the orphaned tenant_blue directly via SQL (bypasses ORM cascade)."""
        self.stdout.write(self.style.MIGRATE_HEADING("\n── Step 1: Delete orphaned tenant_blue ──"))

        with connection.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute("SELECT id FROM core_tenant WHERE schema_name = 'tenant_blue'")
            row = cur.fetchone()
            if not row:
                self.stdout.write("  tenant_blue not found. Skipping.")
                return

            tenant_id = row[0]
            if dry_run:
                self.stdout.write(f"  DRY RUN: Would delete tenant_blue (id={tenant_id}) and its domains")
            else:
                cur.execute("DELETE FROM core_domain WHERE tenant_id = %s", [tenant_id])
                # Get the company_id for cascade cleanup
                cur.execute("SELECT company_id FROM core_tenant WHERE id = %s", [tenant_id])
                company_row = cur.fetchone()
                if company_row and company_row[0]:
                    company_id = company_row[0]
                    # Clean up subscriptions referencing this company
                    cur.execute(
                        "DELETE FROM subscriptions_billingcycle WHERE subscription_id IN "
                        "(SELECT id FROM subscriptions_companysubscription WHERE company_id = %s)",
                        [company_id]
                    )
                    cur.execute("DELETE FROM subscriptions_companysubscription WHERE company_id = %s", [company_id])
                cur.execute("DELETE FROM core_tenant WHERE id = %s", [tenant_id])
                self.stdout.write(self.style.SUCCESS(f"  Deleted tenant_blue (id={tenant_id})"))

    def _delete_branch_b_files(self, dry_run):
        """Delete Branch B migration files from disk."""
        self.stdout.write(self.style.MIGRATE_HEADING("\n── Step 2: Delete Branch B migration files ──"))

        import django
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        ))))  # Goes up from commands/ to project root

        deleted = 0
        for app_label, files in FILES_TO_DELETE.items():
            app_path = APP_PATHS.get(app_label)
            if not app_path:
                continue

            for fname in files:
                full_path = os.path.join(base_dir, app_path, fname)
                if os.path.exists(full_path):
                    if dry_run:
                        self.stdout.write(f"  DRY RUN: Would delete {app_path}/{fname}")
                    else:
                        os.remove(full_path)
                        self.stdout.write(f"  Deleted: {app_path}/{fname}")
                    deleted += 1
                    # Also remove .pyc
                    pyc_path = full_path.replace('.py', '.pyc')
                    if os.path.exists(pyc_path):
                        if not dry_run:
                            os.remove(pyc_path)
                    # Check __pycache__
                    pycache_dir = os.path.join(os.path.dirname(full_path), '__pycache__')
                    if os.path.isdir(pycache_dir):
                        for cached in os.listdir(pycache_dir):
                            if cached.startswith(fname.replace('.py', '.')):
                                cached_path = os.path.join(pycache_dir, cached)
                                if not dry_run:
                                    os.remove(cached_path)

        self.stdout.write(f"  {'Would delete' if dry_run else 'Deleted'} {deleted} file(s)")

    def _purge_branch_b_records(self, dry_run):
        """Remove Branch B migration records from django_migrations in ALL schemas."""
        self.stdout.write(self.style.MIGRATE_HEADING("\n── Step 3: Purge Branch B records from all schemas ──"))

        from apps.core.models import Tenant

        # Get all schema names
        schemas = ['public']
        with connection.cursor() as cur:
            cur.execute("SET search_path TO public")
            # Get existing schemas to avoid setting search_path to non-existent ones
            cur.execute("SELECT schema_name FROM information_schema.schemata")
            existing = {row[0] for row in cur.fetchall()}

        tenant_schemas = list(Tenant.objects.values_list('schema_name', flat=True))
        schemas.extend([s for s in tenant_schemas if s in existing])

        total_purged = 0
        for schema in schemas:
            purged = 0
            for app_label, names in BRANCH_B_NAMES.items():
                for name in names:
                    if dry_run:
                        with connection.cursor() as cur:
                            cur.execute(f'SET search_path TO "{schema}"')
                            cur.execute(
                                "SELECT COUNT(*) FROM django_migrations WHERE app = %s AND name = %s",
                                [app_label, name]
                            )
                            if cur.fetchone()[0] > 0:
                                self.stdout.write(f"  DRY RUN [{schema}]: Would purge {app_label}.{name}")
                                purged += 1
                    else:
                        with connection.cursor() as cur:
                            cur.execute(f'SET search_path TO "{schema}"')
                            cur.execute(
                                "DELETE FROM django_migrations WHERE app = %s AND name = %s",
                                [app_label, name]
                            )
                            if cur.rowcount > 0:
                                purged += cur.rowcount

            if purged > 0:
                self.stdout.write(f"  {'Would purge' if dry_run else 'Purged'} {purged} record(s) from {schema}")
                total_purged += purged

        if total_purged == 0:
            self.stdout.write("  No Branch B records found in any schema.")

    def _fake_equivalent_migrations(self, dry_run):
        """Fake Branch A migrations whose changes are already in the DB."""
        self.stdout.write(self.style.MIGRATE_HEADING("\n── Step 4: Fake equivalent Branch A migrations ──"))

        from apps.core.models import Tenant

        schemas = ['public']
        with connection.cursor() as cur:
            cur.execute("SET search_path TO public")
            cur.execute("SELECT schema_name FROM information_schema.schemata")
            existing = {row[0] for row in cur.fetchall()}

        tenant_schemas = list(Tenant.objects.values_list('schema_name', flat=True))
        schemas.extend([s for s in tenant_schemas if s in existing])

        total_faked = 0
        for schema in schemas:
            faked = 0
            for app_label, names in MIGRATIONS_TO_FAKE.items():
                for name in names:
                    with connection.cursor() as cur:
                        cur.execute(f'SET search_path TO "{schema}"')
                        cur.execute(
                            "SELECT COUNT(*) FROM django_migrations WHERE app = %s AND name = %s",
                            [app_label, name]
                        )
                        already_recorded = cur.fetchone()[0] > 0

                    if already_recorded:
                        continue

                    if dry_run:
                        self.stdout.write(f"  DRY RUN [{schema}]: Would fake {app_label}.{name}")
                    else:
                        with connection.cursor() as cur:
                            cur.execute(f'SET search_path TO "{schema}"')
                            cur.execute(
                                "INSERT INTO django_migrations (app, name, applied) VALUES (%s, %s, NOW())",
                                [app_label, name]
                            )
                            self.stdout.write(self.style.SUCCESS(f"  [{schema}] Faked: {app_label}.{name}"))
                    faked += 1

            total_faked += faked

        if total_faked == 0:
            self.stdout.write("  All equivalent migrations already recorded.")
        else:
            self.stdout.write(f"  {'Would fake' if dry_run else 'Faked'} {total_faked} record(s) across all schemas")
