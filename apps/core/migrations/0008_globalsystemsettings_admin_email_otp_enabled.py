from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0007_loginotpchallenge"),
    ]

    operations = [
        migrations.AddField(
            model_name="globalsystemsettings",
            name="admin_email_otp_enabled",
            field=models.BooleanField(default=False),
        ),
    ]

