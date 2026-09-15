from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0014_platform_sms_wallet_ledger'),
    ]

    operations = [
        migrations.AddField(
            model_name='billingcycle',
            name='pending_discount_amount',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12),
        ),
        migrations.AddField(
            model_name='billingcycle',
            name='pending_discount_reason',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='billingcycle',
            name='pending_manual_adjustment_amount',
            field=models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12),
        ),
        migrations.AddField(
            model_name='billingcycle',
            name='pending_manual_adjustment_description',
            field=models.TextField(blank=True, default=''),
        ),
    ]
