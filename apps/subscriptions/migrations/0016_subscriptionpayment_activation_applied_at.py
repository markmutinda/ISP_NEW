from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('subscriptions', '0015_billingcycle_pending_invoice_adjustments')]

    operations = [
        migrations.AddField(
            model_name='subscriptionpayment',
            name='activation_applied_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
