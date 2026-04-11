# Generated manually for billing lifecycle fields

from django.db import migrations, models
from decimal import Decimal


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0002_companysubscription_converted_from_trial_at_and_more'),
    ]

    operations = [
        # NetilyPlan: Add PPPoE minimum client floor
        migrations.AddField(
            model_name='netilyplan',
            name='pppoe_min_clients',
            field=models.PositiveIntegerField(
                default=20,
                help_text='Minimum billable PPPoE clients per cycle (floor). ISPs with fewer active clients are still billed for this many.',
            ),
        ),

        # BillingCycle: Add snapshot of min clients floor
        migrations.AddField(
            model_name='billingcycle',
            name='snapshot_min_clients',
            field=models.PositiveIntegerField(
                default=20,
                help_text='Minimum billable PPPoE clients (floor)',
            ),
        ),

        # BillingCycle: Add grace period end tracking
        migrations.AddField(
            model_name='billingcycle',
            name='grace_ends_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text='When the 4-day grace period expires. Set when invoice is generated.',
            ),
        ),
    ]
