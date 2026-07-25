from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0020_user_custom_allowed_paths"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="country",
            field=models.CharField(
                choices=[("KE", "Kenya"), ("GH", "Ghana"), ("NG", "Nigeria"), ("TZ", "Tanzania"), ("UG", "Uganda")],
                default="KE",
                max_length=2,
            ),
        ),
        migrations.AddField(
            model_name="company",
            name="base_currency",
            field=models.CharField(default="KES", max_length=3),
        ),
    ]