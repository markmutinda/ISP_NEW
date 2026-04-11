"""
Management command: populate_loyalty_members

Enrolls ALL existing customers in the loyalty program, calculates
retroactive points from their payment history, and assigns tiers.
Safe to run multiple times — skips already-enrolled customers.
"""
from django.core.management.base import BaseCommand
from django.db import connection
from django_tenants.utils import get_tenant_model, tenant_context


class Command(BaseCommand):
    help = 'Populate loyalty members for all existing customers across all tenants'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant', type=str, default=None,
            help='Run for a specific tenant schema name only'
        )
        parser.add_argument(
            '--retroactive', action='store_true', default=True,
            help='Calculate retroactive points from payment history'
        )
        parser.add_argument(
            '--dry-run', action='store_true', default=False,
            help='Preview what would happen without making changes'
        )

    def handle(self, *args, **options):
        TenantModel = get_tenant_model()
        tenants = TenantModel.objects.exclude(schema_name='public')
        if options['tenant']:
            tenants = tenants.filter(schema_name=options['tenant'])

        for tenant in tenants:
            self.stdout.write(f'\n=== Tenant: {tenant.schema_name} ({tenant.name}) ===')
            with tenant_context(tenant):
                self._populate_tenant(
                    retroactive=options['retroactive'],
                    dry_run=options['dry_run'],
                )

    def _populate_tenant(self, retroactive=True, dry_run=False):
        from apps.loyalty.models import LoyaltySettings, LoyaltyTier, LoyaltyMember
        from apps.customers.models import Customer

        # Ensure settings exist
        settings_obj = LoyaltySettings.load()

        # Ensure default tiers exist
        default_tiers = [
            {'name': 'Bronze', 'level': 'bronze', 'min_points': 0, 'max_points': 499,
             'points_multiplier': '1.00', 'color': 'bg-amber-500',
             'benefits': ['Basic support', '1x points earning']},
            {'name': 'Silver', 'level': 'silver', 'min_points': 500, 'max_points': 1999,
             'points_multiplier': '1.25', 'color': 'bg-slate-400',
             'benefits': ['Priority support', '1.25x points', '5% plan discount']},
            {'name': 'Gold', 'level': 'gold', 'min_points': 2000, 'max_points': 4999,
             'points_multiplier': '1.50', 'color': 'bg-yellow-500',
             'benefits': ['24/7 support', '1.5x points', '10% plan discount', 'Free speed boost (1 day/month)']},
            {'name': 'Platinum', 'level': 'platinum', 'min_points': 5000, 'max_points': 14999,
             'points_multiplier': '2.00', 'color': 'bg-slate-600',
             'benefits': ['Dedicated account manager', '2x points', '15% plan discount', 'Free speed boost (3 days/month)', 'Early access to new plans']},
            {'name': 'Diamond', 'level': 'diamond', 'min_points': 15000, 'max_points': None,
             'points_multiplier': '3.00', 'color': 'bg-cyan-500',
             'benefits': ['VIP support', '3x points', '25% plan discount', 'Unlimited speed boosts', 'Free plan upgrades', 'Referral super-bonus']},
        ]

        for td in default_tiers:
            tier, created = LoyaltyTier.objects.get_or_create(
                level=td['level'],
                defaults=td,
            )
            if created:
                self.stdout.write(f'  Created tier: {tier.name}')

        # Enroll customers
        customers = Customer.objects.all()
        enrolled = 0
        skipped = 0
        total_retro_points = 0

        for customer in customers:
            member, was_created = LoyaltyMember.objects.get_or_create(
                customer=customer,
                defaults={
                    'tier': LoyaltyTier.objects.filter(level='bronze').first(),
                    'joined_date': customer.created_at,
                }
            )

            if not was_created:
                skipped += 1
                continue

            enrolled += 1

            # Retroactive points from payment history
            if retroactive:
                retro_points = self._calculate_retroactive_points(customer, member, settings_obj, dry_run)
                total_retro_points += retro_points

            # Recalculate tier
            if not dry_run:
                member.recalculate_tier()

        self.stdout.write(
            f'  Enrolled: {enrolled} new members, Skipped: {skipped} existing, '
            f'Retro points: {total_retro_points:,}'
        )

    def _calculate_retroactive_points(self, customer, member, settings_obj, dry_run):
        """Calculate points from historical payments."""
        from apps.billing.models.payment_models import Payment
        from decimal import Decimal

        payments = Payment.objects.filter(
            customer=customer,
            status='COMPLETED'
        ).order_by('created_at')

        total_points = 0
        total_amount = Decimal('0.00')

        for payment in payments:
            amount = float(payment.amount)
            if settings_obj.currency_unit > 0:
                points = int(amount / settings_obj.currency_unit) * settings_obj.points_per_currency
            else:
                points = 0
            total_points += points
            total_amount += payment.amount

        if total_points > 0 and not dry_run:
            member.current_points = total_points
            member.lifetime_points = total_points
            member.total_spent = total_amount
            member.total_payments = payments.count()
            member.save(update_fields=[
                'current_points', 'lifetime_points',
                'total_spent', 'total_payments', 'updated_at',
            ])
            # Create one summary transaction
            from apps.loyalty.models import PointsTransaction
            PointsTransaction.objects.create(
                member=member,
                transaction_type='bonus',
                points=total_points,
                description=f'Retroactive points from {payments.count()} historical payments (KES {total_amount:,.2f})',
            )

        return total_points
