from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0026_globalsystemsettings_appearance_font"),
    ]

    operations = [
        migrations.AlterField(
            model_name="emailotp",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("login", "Tenant Login"),
                    ("payment_method_change", "Payment Method Verification"),
                    ("affiliate_password_reset", "Affiliate Password Reset"),
                ],
                db_index=True,
                default="login",
                max_length=40,
            ),
        ),
    ]
