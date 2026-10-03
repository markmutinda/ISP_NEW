from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.subscriptions.models import BillingCycle, CompanySubscription


class Command(BaseCommand):
    help = (
        "Repair duplicate active subscription billing cycles and align the "
        "subscription current period with the selected active cycle."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without updating billing cycles.",
        )
        parser.add_argument(
            "--company",
            action="append",
            default=[],
            help="Filter by company or tenant name. Can be supplied multiple times.",
        )
        parser.add_argument(
            "--tenant",
            action="append",
            default=[],
            help="Filter by tenant schema, subdomain, domain, database name, or company name. Can be supplied multiple times.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=200,
            help="Maximum subscriptions to inspect. Default: 200.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        companies = [str(v).strip() for v in options.get("company") or [] if str(v).strip()]
        tenants = [str(v).strip() for v in options.get("tenant") or [] if str(v).strip()]
        limit = max(1, int(options.get("limit") or 200))

        repaired = skipped = previewed = 0

        with schema_context(get_public_schema_name()):
            qs = (
                CompanySubscription.objects.select_related("company", "company__tenant", "plan")
                .filter(company__tenant__isnull=False)
                .order_by("company__name", "id")
            )
            if companies:
                company_q = None
                from django.db.models import Q

                for company in companies:
                    q = Q(company__name__icontains=company)
                    company_q = q if company_q is None else company_q | q
                qs = qs.filter(company_q)
            if tenants:
                tenant_q = None
                from django.db.models import Q

                for tenant in tenants:
                    q = (
                        Q(company__tenant__schema_name__icontains=tenant)
                        | Q(company__tenant__subdomain__icontains=tenant)
                        | Q(company__tenant__domain__icontains=tenant)
                        | Q(company__tenant__database_name__icontains=tenant)
                        | Q(company__name__icontains=tenant)
                    )
                    tenant_q = q if tenant_q is None else tenant_q | q
                qs = qs.filter(tenant_q)

            for subscription in qs[:limit]:
                tenant = subscription.company.tenant
                active_cycles = list(
                    BillingCycle.objects.filter(
                        tenant=tenant,
                        subscription=subscription,
                        status="active",
                    ).order_by("-start_date", "-end_date", "-id")
                )
                if not active_cycles:
                    skipped += 1
                    continue

                target = _select_target_cycle(subscription, active_cycles)
                stale = [cycle for cycle in active_cycles if cycle.pk != target.pk]

                needs_subscription_alignment = (
                    target.start_date != subscription.current_period_start
                    or target.end_date != subscription.current_period_end
                )
                if not stale and not needs_subscription_alignment:
                    skipped += 1
                    continue

                label = (
                    f"{subscription.company.name} | tenant={tenant.schema_name} | "
                    f"keep={target.id} {target.start_date} -> {target.end_date} | "
                    f"close={len(stale)} | align_subscription={bool(needs_subscription_alignment)}"
                )
                if dry_run:
                    previewed += 1
                    self.stdout.write(f"Would repair: {label}")
                    continue

                with transaction.atomic():
                    if needs_subscription_alignment:
                        CompanySubscription.objects.filter(pk=subscription.pk).update(
                            current_period_start=target.start_date,
                            current_period_end=target.end_date,
                            status="active",
                            updated_at=timezone.now(),
                        )
                    if stale:
                        BillingCycle.objects.filter(pk__in=[cycle.pk for cycle in stale]).update(status="paid")
                    BillingCycle.normalize_active_cycles(tenant, subscription, preferred_cycle=target)

                repaired += 1
                self.stdout.write(self.style.SUCCESS(f"Repaired: {label}"))

        summary = f"billing cycle repair finished: previewed={previewed}, repaired={repaired}, skipped={skipped}"
        self.stdout.write(self.style.SUCCESS(summary))


def _select_target_cycle(subscription, active_cycles):
    # Prefer the longest active window; one-day duplicates should lose.
    # If two rows have the same span, prefer the newest period.
    return sorted(
        active_cycles,
        key=lambda cycle: (
            cycle.end_date - cycle.start_date,
            cycle.start_date,
            cycle.end_date,
        ),
        reverse=True,
    )[0]
