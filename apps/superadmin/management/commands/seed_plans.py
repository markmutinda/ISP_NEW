"""
Seed the correct Netily platform subscription plans.

Two plans are managed:
  Starter   — metered/usage-based (KES 500 one-time activation, KES 25/PPPoE user,
               3% hotspot revenue share, KES 500/month minimum). This is the plan
               shown in the BillingCalculator on the homepage.
  Enterprise — custom/contact-sales pricing for large ISPs.

Usage:
    python manage.py seed_plans          # Create/update Starter + Enterprise
    python manage.py seed_plans --force  # Delete all plans and recreate
"""
from django.core.management.base import BaseCommand
from decimal import Decimal


PLANS = [
    {
        # Starter = the metered / pay-as-you-grow plan shown in the BillingCalculator.
        # Billing = KES 500 activation (one-time at trial end) +
        #           KES 25 per active PPPoE user per month +
        #           3% of hotspot revenue, minimum KES 500 / month.
        "name": "Starter",
        "code": "starter",
        "tagline": "Perfect for growing ISPs who want to keep costs lean.",
        "description": (
            "Pay only for what you use. KES 500 one-time activation after your free trial, "
            "then KES 25 per active PPPoE subscriber + 3% of hotspot revenue each month. "
            "Minimum monthly charge: KES 500."
        ),
        # price_monthly here represents the minimum / base floor shown in the UI;
        # actual billing is metered and calculated at cycle end.
        "price_monthly": Decimal("500.00"),
        "price_yearly": Decimal("0.00"),   # metered — no annual flat rate
        "is_metered": True,
        "activation_fee": Decimal("500.00"),
        "base_license_fee": Decimal("500.00"),  # KES 500 minimum monthly floor
        "pppoe_unit_price": Decimal("25.00"),   # KES 25 per active PPPoE user
        "pppoe_min_clients": 0,                 # no PPPoE client floor; floor is base_license_fee
        "hotspot_revenue_share_pct": Decimal("3.00"),
        "max_subscribers": 0,   # unlimited
        "max_routers": 0,       # unlimited
        "max_staff": 0,         # unlimited
        "features": [
            "Free M-Pesa STK Push integration",
            "MikroTik auto-provisioning",
            "Unlimited routers & subscribers",
            "PPPoE & Hotspot billing",
            "Real-time bandwidth management",
            "Analytics & reports",
            "Email & chat support",
        ],
        "is_active": True,
        "is_popular": True,
        "sort_order": 0,
    },
    {
        # Enterprise = custom / contact-sales plan for large ISPs.
        # price_monthly is stored as 0 because pricing is agreed per-customer;
        # the platform superadmin configures the actual rates manually.
        "name": "Enterprise",
        "code": "enterprise",
        "tagline": "Scale without limits — pricing tailored to your ISP.",
        "description": (
            "White-label, SLA guarantee, and pricing built around your ISP. "
            "Dedicated account manager, API access, and custom billing rates. "
            "Contact the Netily team to get started."
        ),
        "price_monthly": Decimal("0.00"),   # custom — set per customer
        "price_yearly": Decimal("0.00"),    # custom
        "is_metered": True,                 # still usage-based; rates negotiated
        "activation_fee": Decimal("500.00"),
        "base_license_fee": Decimal("500.00"),
        "pppoe_unit_price": Decimal("25.00"),   # default; overridden per tenant
        "pppoe_min_clients": 0,
        "hotspot_revenue_share_pct": Decimal("3.00"),
        "max_subscribers": 0,   # unlimited
        "max_routers": 0,       # unlimited
        "max_staff": 0,         # unlimited
        "features": [
            "Everything in Starter",
            "Unlimited subscribers & routers",
            "Unlimited staff accounts",
            "Dedicated account manager",
            "24/7 priority support",
            "Custom billing rates",
            "White-label options",
            "API access",
            "SLA guarantee",
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
