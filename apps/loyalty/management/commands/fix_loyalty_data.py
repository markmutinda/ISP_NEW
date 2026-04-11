"""
Management command: fix_loyalty_data

Fixes loyalty data for all tenants:
1. Updates tier thresholds to ISP-friendly values
2. Awards signup bonus to members with 0 lifetime points
3. Recalculates tiers for all members
4. Enrolls any unenrolled customers

Safe to run multiple times.
"""
from django.core.management.base import BaseCommand
from django_tenants.utils import get_tenant_model, tenant_context


class Command(BaseCommand):
    help = 'Fix loyalty tiers, award missing signup bonuses, and recalculate tiers'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant', type=str, default=None,
            help='Run for a specific tenant schema name only'
        )

    def handle(self, *args, **options):
        TenantModel = get_tenant_model()
        tenants = TenantModel.objects.exclude(schema_name='public')
        if options['tenant']:
            tenants = tenants.filter(schema_name=options['tenant'])

        for tenant in tenants:
            label = getattr(tenant, 'name', None) or tenant.schema_name
            self.stdout.write(f'\n=== Tenant: {tenant.schema_name} ({label}) ===')
            with tenant_context(tenant):
                self._fix_tenant()

    def _fix_tenant(self):
        from apps.loyalty.models import LoyaltySettings, LoyaltyTier, LoyaltyMember, PointsTransaction
        from apps.customers.models import Customer

        settings_obj = LoyaltySettings.load()

        # ── 1. Fix tier thresholds ──────────────────────────────
        tier_updates = {
            'bronze':   {'min_points': 0,    'max_points': 49},
            'silver':   {'min_points': 50,   'max_points': 199},
            'gold':     {'min_points': 200,  'max_points': 499},
            'platinum': {'min_points': 500,  'max_points': 999},
            'diamond':  {'min_points': 1000, 'max_points': None},
        }
        for level, vals in tier_updates.items():
            updated = LoyaltyTier.objects.filter(level=level).update(**vals)
            if updated:
                self.stdout.write(f'  Updated {level}: {vals["min_points"]}-{vals["max_points"] or "∞"} pts')

        # Ensure all tiers exist
        tier_defaults = [
            {'name': 'Bronze', 'level': 'bronze', 'min_points': 0, 'max_points': 49,
             'points_multiplier': '1.00', 'color': 'bg-amber-500',
             'benefits': ['Basic support', '1x points earning']},
            {'name': 'Silver', 'level': 'silver', 'min_points': 50, 'max_points': 199,
             'points_multiplier': '1.25', 'color': 'bg-slate-400',
             'benefits': ['Priority support', '1.25x points', '5% plan discount']},
            {'name': 'Gold', 'level': 'gold', 'min_points': 200, 'max_points': 499,
             'points_multiplier': '1.50', 'color': 'bg-yellow-500',
             'benefits': ['24/7 support', '1.5x points', '10% plan discount', 'Free speed boost (1 day/month)']},
            {'name': 'Platinum', 'level': 'platinum', 'min_points': 500, 'max_points': 999,
             'points_multiplier': '2.00', 'color': 'bg-slate-600',
             'benefits': ['Dedicated account manager', '2x points', '15% plan discount']},
            {'name': 'Diamond', 'level': 'diamond', 'min_points': 1000, 'max_points': None,
             'points_multiplier': '3.00', 'color': 'bg-cyan-500',
             'benefits': ['VIP support', '3x points', '25% plan discount']},
        ]
        for td in tier_defaults:
            LoyaltyTier.objects.get_or_create(level=td['level'], defaults=td)

        # ── 2. Enroll unenrolled customers ──────────────────────
        bronze = LoyaltyTier.objects.filter(level='bronze').first()
        enrolled = 0
        for customer in Customer.objects.all():
            _, created = LoyaltyMember.objects.get_or_create(
                customer=customer,
                defaults={'tier': bronze, 'joined_date': customer.created_at},
            )
            if created:
                enrolled += 1
        if enrolled:
            self.stdout.write(f'  Enrolled {enrolled} new members')

        # ── 3. Award signup bonus to members with 0 points ─────
        bonus_awarded = 0
        if settings_obj.signup_bonus > 0:
            zero_members = LoyaltyMember.objects.filter(lifetime_points=0)
            for member in zero_members:
                member.award_points(
                    points=settings_obj.signup_bonus,
                    description='Welcome bonus (retroactive fix)',
                    transaction_type='bonus',
                )
                bonus_awarded += 1
        self.stdout.write(f'  Awarded signup bonus to {bonus_awarded} members')

        # ── 4. Calculate retroactive points from payments ──────
        retro_fixed = 0
        for member in LoyaltyMember.objects.all():
            retro = self._calc_retro(member, settings_obj)
            if retro > 0:
                retro_fixed += 1
        if retro_fixed:
            self.stdout.write(f'  Calculated retroactive payment points for {retro_fixed} members')

        # ── 5. Recalculate tiers for all members ───────────────
        upgraded = 0
        for member in LoyaltyMember.objects.all():
            old_tier, new_tier = member.recalculate_tier()
            if new_tier:
                upgraded += 1
        self.stdout.write(f'  Tier upgrades: {upgraded}')

        total = LoyaltyMember.objects.count()
        self.stdout.write(self.style.SUCCESS(f'  Done! Total members: {total}'))

    def _calc_retro(self, member, settings_obj):
        from apps.billing.models.payment_models import Payment
        from decimal import Decimal

        payments = Payment.objects.filter(
            customer=member.customer,
            status='COMPLETED',
        )
        if not payments.exists():
            return 0

        total_amount = Decimal('0.00')
        total_points = 0
        for p in payments:
            amount = float(p.amount)
            if settings_obj.currency_unit > 0:
                total_points += int(amount / settings_obj.currency_unit) * settings_obj.points_per_currency
            total_amount += p.amount

        # Only update if we calculated more than what was already tracked
        if total_points > 0 and member.total_payments < payments.count():
            member.total_spent = total_amount
            member.total_payments = payments.count()
            member.save(update_fields=['total_spent', 'total_payments', 'updated_at'])

            # Award the difference in points
            existing = member.lifetime_points
            if total_points > existing:
                diff = total_points - existing
                member.award_points(
                    points=diff,
                    description=f'Retroactive: {payments.count()} payments (KES {total_amount:,.2f})',
                    transaction_type='earned',
                )
                return diff
        return 0
