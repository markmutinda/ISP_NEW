from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("subscriptions", "0007_subscriptionpayment_defer_billing_to_trial_end"),
    ]

    operations = [
        migrations.AlterField(
            model_name="netilyplan",
            name="pppoe_unit_price",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("25.00"),
                max_digits=10,
            ),
        ),
    ]
