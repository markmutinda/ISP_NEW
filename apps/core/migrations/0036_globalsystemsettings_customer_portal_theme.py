from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0035_remove_user_uniq_customer_phone_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="globalsystemsettings",
            name="customer_portal_theme",
            field=models.CharField(blank=True, default="classic", max_length=32),
        ),
    ]
