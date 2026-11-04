from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("subscriptions", "0017_subscriptionpayment_business_account")]

    operations = [
        migrations.AddField(
            model_name="netilyplan",
            name="is_selfie_activation_plan",
            field=models.BooleanField(default=False, help_text="Use this plan and its activation fee for Superadmin Selfie-2 tenant registrations."),
        ),
    ]
