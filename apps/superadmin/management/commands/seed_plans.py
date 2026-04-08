"""
Seed the correct Netily platform subscription plans.

Usage:
    python manage.py seed_plans          # Create/update all 4 plans
    python manage.py seed_plans --force  # Delete existing plans and recreate
"""
from django.core.management.base import BaseCommand
from decimal import Decimal


PLANS = [
    {
        "name": "Metered",
        "code": "metered",
        "tagline": "Pay as you grow — perfect for new ISPs",
        "description": (
            "Usage-based pricing that scales with your business. "
            "Base fee of KES 500/mo plus KES 20 per active PPPoE client "
            "and 3% of hotspot revenue."
        ),
        "price_monthly": Decimal("500.00"),   # base only; actual bill is metered
        "price_yearly": Decimal("5400.00"),    # ~10% discount on base
        "is_metered": True,
        "base_license_fee": Decimal("500.00"),
        "pppoe_unit_price": Decimal("20.00"),
        "pppoe_min_clients": 20,
        "hotspot_revenue_share_pct": Decimal("3.00"),
        "max_subscribers": 0,   # unlimited
        "max_routers": 0,       # unlimited
        "max_staff": 5,
        "features": [
            "Unlimited subscribers",
            "Unlimited routers",
            "Up to 5 staff accounts",
            "PPPoE & Hotspot billing",
            "Basic analytics",
            "Email support",
        ],
        "is_active": True,
        "is_popular": False,
        "sort_order": 0,
    },
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
        "base_license_fee": Decimal("500.00"),
        "pppoe_unit_price": Decimal("20.00"),
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
        "sort_order": 1,
    },
    {
        "name": "Professional",
        "code": "professional",
        "tagline": "Scale with confidence",
        "description": (
            "Flat KES 7,999/mo for established ISPs. "
            "Up to 1,000 subscribers, 50 routers, unlimited staff. "
            "Includes advanced analytics and priority support."
        ),
        "price_monthly": Decimal("7999.00"),
        "price_yearly": Decimal("79990.00"),   # ~2 months free
        "is_metered": False,
        "base_license_fee": Decimal("500.00"),
        "pppoe_unit_price": Decimal("20.00"),
        "pppoe_min_clients": 20,
        "hotspot_revenue_share_pct": Decimal("3.00"),
        "max_subscribers": 1000,
        "max_routers": 50,
        "max_staff": 0,   # unlimited
        "features": [
            "Up to 1,000 subscribers",
            "Up to 50 routers",
            "Unlimited staff accounts",
            "PPPoE & Hotspot billing",
            "Advanced analytics & reports",
            "Priority support",
            "Network monitoring",
            "Bandwidth management",
            "Custom branding",
        ],
        "is_active": True,
        "is_popular": True,
        "sort_order": 2,
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
        "base_license_fee": Decimal("500.00"),
        "pppoe_unit_price": Decimal("20.00"),
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
        "sort_order": 3,
    },
]


class Command(BaseCommand):
    help = "Seed the 4 Netily subscription plans (Metered, Starter, Professional, Enterprise)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Delete ALL existing plans and recreate from scratch",
        )

    def handle(self, *args, **options):
        from apps.subscriptions.models import NetilyPlan

        if options["force"]:
            deleted, _ = NetilyPlan.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {deleted} existing plans."))

        # Always clean up old/junk plans that don't match the 4 valid codes
        valid_codes = [p["code"] for p in PLANS]
        junk = NetilyPlan.objects.exclude(code__in=valid_codes)
        if junk.exists():
            names = list(junk.values_list("name", "code"))
            junk.delete()
            for name, code in names:
                self.stdout.write(self.style.WARNING(f"  Removed old plan: {name} ({code})"))

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

        # Verify prices were saved correctly
        self.stdout.write("\n  Verification:")
        for p in NetilyPlan.objects.all().order_by("sort_order"):
            self.stdout.write(
                f"    {p.name} ({p.code}): "
                f"monthly=KES {p.price_monthly}, "
                f"yearly=KES {p.price_yearly}, "
                f"metered={p.is_metered}"
            )

        self.stdout.write(self.style.SUCCESS(
            f"\nDone — {created} created, {updated} updated. "
            f"Total plans: {NetilyPlan.objects.count()}"
        ))
