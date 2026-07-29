from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0022_alter_company_base_currency_alter_company_country"),
    ]

    operations = [
        migrations.AddField(
            model_name="globalsystemsettings",
            name="affiliate_email_otp_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
