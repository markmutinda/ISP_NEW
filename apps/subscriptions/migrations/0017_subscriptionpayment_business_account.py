from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("subscriptions", "0016_subscriptionpayment_activation_applied_at")]

    operations = [
        migrations.AddField(
            model_name="subscriptionpayment",
            name="business_account",
            field=models.CharField(
                max_length=32, null=True, blank=True, db_index=True,
                choices=[("primary", "Original Business Account"), ("new_business", "New Business Account")],
                help_text="Explicit allocation for manually recorded payments; legacy payments use the cutover date.",
            ),
        ),
    ]
