"""
Recalculate hotspot_revenue_accumulated from actual hotspot revenue.

Instead of trusting the running accumulator (which can drift due to bugs,
double-counting, or missed decrements), this command uses the same
payment-first reconciliation logic as the subscription usage API.

Applies to ALL tenants by default, or a single tenant with --tenant.

Usage:
    python manage.py fix_hotspot_revenue          # dry-run (default)
    python manage.py fix_hotspot_revenue --apply   # actually update
    python manage.py fix_hotspot_revenue --apply --tenant pink4
"""
from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context, get_public_schema_name


class Command(BaseCommand):
    help = "Recalculate hotspot_revenue_accumulated from actual hotspot revenue for all tenants."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write the fix. Without this flag, only a dry-run report is printed.",
        )
        parser.add_argument(
            "--tenant",
            type=str,
            default=None,
            help="Limit to a single tenant schema name (e.g. pink4).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        tenant_filter = options.get("tenant")

        from apps.subscriptions.models import BillingCycle

        with schema_context(get_public_schema_name()):
            qs = BillingCycle.objects.filter(
                status__in=['active', 'invoiced'],
            ).select_related("tenant", "subscription__plan")

            if tenant_filter:
                qs = qs.filter(tenant__schema_name=tenant_filter)

            cycles = list(qs)

        if not cycles:
            self.stdout.write(self.style.WARNING("No billing cycles found."))
            return

        fixed_count = 0
        skipped_count = 0

        for cycle in cycles:
            tenant = cycle.tenant
            label = (
                f"[{tenant.schema_name}] Cycle {cycle.start_date.date()}"
                f" -> {cycle.end_date.date()} (status={cycle.status})"
            )

            revenue_details = cycle.get_actual_hotspot_revenue_details()
            actual_revenue = revenue_details["revenue"]
            record_count = revenue_details["count"]
            source = revenue_details["source"]

            old_val = cycle.hotspot_revenue_accumulated
            drift = old_val - actual_revenue

            if old_val == actual_revenue:
                self.stdout.write(
                    f"  [OK] {label}: KES {old_val} -- already correct "
                    f"({record_count} {source})"
                )
                skipped_count += 1
                continue

            if apply:
                with schema_context(get_public_schema_name()):
                    BillingCycle.objects.filter(pk=cycle.pk).update(
                        hotspot_revenue_accumulated=actual_revenue
                    )
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  FIXED {label}: KES {old_val} -> KES {actual_revenue} "
                        f"(drift: {drift:+,.2f}, {record_count} {source})"
                    )
                )
            else:
                self.stdout.write(
                    f"  [DRY-RUN] {label}: KES {old_val} -> KES {actual_revenue} "
                    f"(drift: {drift:+,.2f}, {record_count} {source})"
                )
            fixed_count += 1

        mode = "APPLIED" if apply else "DRY-RUN"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{mode}: {fixed_count} cycle(s) corrected, {skipped_count} already accurate."
            )
        )
        if not apply:
            self.stdout.write(self.style.WARNING("Run again with --apply to commit changes."))
