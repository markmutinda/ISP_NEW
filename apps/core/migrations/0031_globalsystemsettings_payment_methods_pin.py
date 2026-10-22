from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0030_tenantactivationpayment"),
    ]

    operations = [
        migrations.AddField(
            model_name="globalsystemsettings",
            name="payment_methods_pin_hash",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="globalsystemsettings",
            name="payment_methods_pin_updated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
