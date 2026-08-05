from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0025_webauthncredential"),
    ]

    operations = [
        migrations.AddField(
            model_name="globalsystemsettings",
            name="appearance_font",
            field=models.CharField(blank=True, default="outfit", max_length=40),
        ),
    ]
