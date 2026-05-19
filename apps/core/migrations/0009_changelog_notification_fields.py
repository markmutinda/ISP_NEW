from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_globalsystemsettings_admin_email_otp_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="changelog",
            name="notification_channels",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="changelog",
            name="notification_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="changelog",
            name="notification_summary",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
