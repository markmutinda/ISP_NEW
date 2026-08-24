from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("network", "0018_alter_router_last_login_html_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="router",
            name="portal_font",
            field=models.CharField(
                blank=True,
                default="outfit",
                help_text="Typeface used on the captive portal",
                max_length=40,
            ),
        ),
    ]
