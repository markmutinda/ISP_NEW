"""
Seed the correct Netily platform subscription plans.

Only two plans are managed: Starter and Enterprise.
Metered and Professional are no longer offered; existing subscriptions on
those plans are automatically migrated to Starter.

Usage:
    python manage.py seed_plans          # Create/update Starter + Enterprise
    python manage.py seed_plans --force  # Delete all plans and recreate
"""
from django.core.management.base import BaseCommand
from decimal import Decimal


PLANS = [
    {
        "name": "Starter",
        "code": "starter",
        "tagline": "Everything you need to get started",
        "description": (
            "Flat KES 2,999/mo for growing ISPs. "
            "Includes up to 200 subscribers, 10 routers, and 5 staff accounts."
        ),
        "price_monthly": Decimal("2999.00"),
        "price_yearly": Decimal("29990.00"),   # ~2 months free
        "is_metered": False,
        "activation_fee": Decimal("500.00"),
        "base_license_fee": Decimal("500.00"),
        "pppoe_unit_price": Decimal("25.00"),
        "pppoe_min_clients": 20,
        "hotspot_revenue_share_pct": Decimal("3.00"),
        "max_subscribers": 200,
        "max_routers": 10,
        "max_staff": 5,
        "features": [
            "Up to 200 subscribers",
            "Up to 10 routers",
            "Up to 5 staff accounts",
            "PPPoE & Hotspot billing",
            "Standard analytics",
            "Email & chat support",
            "Network monitoring",
        ],
        "is_active": True,
        "is_popular": False,
        "sort_order": 0,
    },
    {
        "name": "Enterprise",
        "code": "enterprise",
        "tagline": "Unlimited power for large ISPs",
        "description": (
            "Flat KES 19,999/mo — everything unlimited. "
            "Dedicated account manager, API access, white-label options."
        ),
        "price_monthly": Decimal("19999.00"),
        "price_yearly": Decimal("199990.00"),  # ~2 months free
        "is_metered": False,
        "activation_fee": Decimal("500.00"),
        "base_license_fee": Decimal("500.00"),
        "pppoe_unit_price": Decimal("25.00"),
        "pppoe_min_clients": 20,
        "hotspot_revenue_share_pct": Decimal("3.00"),
        "max_subscribers": 0,   # unlimited
        "max_routers": 0,       # unlimited
        "max_staff": 0,         # unlimited
        "features": [
            "Unlimited subscribers",
            "Unlimited routers",
            "Unlimited staff accounts",
            "PPPoE & Hotspot billing",
            "Advanced analytics & reports",
            "Dedicated account manager",
            "24/7 priority support",
            "Network monitoring",
            "Bandwidth management",
            "Custom branding",
            "API access",
            "White-label options",
        ],
        "is_active": True,
        "is_popular": False,
        "sort_order": 1,
    },
]


class Command(BaseCommand):
    help = "Seed the 2 Netily subscription plans (Starter, Enterprise)"

    # Mapping: old plan name fragments → target new plan code.
    # Metered and Professional no longer exist; their subscriptions move to Starter.
    OLD_PLAN_MIGRATION_MAP = {
        'metered':      'starter',
        'starter':      'starter',
        'basic':        'starter',
        'free':         'starter',
        'professional': 'starter',
        'pro':          'starter',
        'enterprise':   'enterprise',
    }
    DEFAULT_MIGRATION_TARGET = 'starter'  # Fallback for unrecognised old plan names

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Delete ALL existing plans and recreate from scratch (migrates subscriptions first)",
        )

    def _resolve_replacement(self, old_plan, new_plans_by_code):
        """Pick the best new plan to replace an old one."""
        name_lower = old_plan.name.lower()
        for fragment, new_code in self.OLD_PLAN_MIGRATION_MAP.items():
            if fragment in name_lower:
                target = new_plans_by_code.get(new_code)
                if target:
                    return target
        return new_plans_by_code.get(self.DEFAULT_MIGRATION_TARGET)

    def handle(self, *args, **options):
        from apps.subscriptions.models import BillingCycle, NetilyPlan, CompanySubscription

        # ── STEP 1: Create/update the 2 canonical plans first ─────────────────
        created = 0
        updated = 0
        for plan_data in PLANS:
            code = plan_data["code"]
            obj, was_created = NetilyPlan.objects.update_or_create(
                code=code,
                defaults=plan_data,
            )
            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"  Created: {obj.name} ({obj.code})"))
            else:
                updated += 1
                self.stdout.write(self.style.HTTP_INFO(f"  Updated: {obj.name} ({obj.code})"))

        # Reload the canonical plans for the migration map
        valid_codes = [p["code"] for p in PLANS]
        new_plans_by_code = {p.code: p for p in NetilyPlan.objects.filter(code__in=valid_codes)}

        active_cycle_updates = BillingCycle.objects.filter(
            status="active",
            snapshot_pppoe_price=Decimal("20.00"),
        ).update(snapshot_pppoe_price=Decimal("25.00"))
        if active_cycle_updates:
            self.stdout.write(
                self.style.HTTP_INFO(
                    f"  Updated {active_cycle_updates} active billing cycle snapshot(s) "
                    "from KES 20.00 to KES 25.00"
                )
            )

        # ── STEP 2: Migrate subscriptions off old/junk plans ──────────────────
        junk_plans = NetilyPlan.objects.exclude(code__in=valid_codes)
        junk_count = junk_plans.count()

        if junk_count:
            self.stdout.write(f"\n  Found {junk_count} old plan(s) to migrate away from:")
            for old_plan in junk_plans:
                replacement = self._resolve_replacement(old_plan, new_plans_by_code)
                if not replacement:
                    self.stdout.write(
                        self.style.ERROR(
                            f"    ✗ Could not find a replacement for '{old_plan.name}'. "
                            f"Subscriptions will NOT be migrated. Add a mapping in OLD_PLAN_MIGRATION_MAP."
                        )
                    )
                    continue

                subs = CompanySubscription.objects.filter(plan=old_plan)
                sub_count = subs.count()
                if sub_count:
                    # Migrate all subscriptions to the replacement plan
                    subs.update(plan=replacement)
                    self.stdout.write(
                        self.style.WARNING(
                            f"    ↳ Migrated {sub_count} subscription(s) from "
                            f"'{old_plan.name}' → '{replacement.name}'"
                        )
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING(f"    ↳ '{old_plan.name}' — no subscriptions, will delete")
                    )

            # ── STEP 3: Delete junk plans (safe now — no protected FKs) ───────
            deleted_names = list(junk_plans.values_list("name", flat=True))
            junk_plans.delete()
            for name in deleted_names:
                self.stdout.write(self.style.WARNING(f"  Removed old plan: {name}"))

        # ── STEP 4: Verify ────────────────────────────────────────────────────
        self.stdout.write("\n  Final plan table:")
        for p in NetilyPlan.objects.all().order_by("sort_order"):
            sub_cnt = CompanySubscription.objects.filter(plan=p).count()
            self.stdout.write(
                f"    {p.name} ({p.code}): "
                f"monthly=KES {p.price_monthly}, "
                f"yearly=KES {p.price_yearly}, "
                f"metered={p.is_metered}, "
                f"subscribers={sub_cnt}"
            )

        self.stdout.write(self.style.SUCCESS(
            f"\nDone — {created} created, {updated} updated. "
            f"Total plans: {NetilyPlan.objects.count()}"
        ))
