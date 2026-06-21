from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("superadmin", "0002_support_executives"),
    ]

    operations = [
        migrations.CreateModel(
            name="SuperAdminActivityLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=80)),
                ("summary", models.CharField(max_length=255)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="superadmin_actions_performed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "target_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="superadmin_actions_received",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="superadminactivitylog",
            index=models.Index(fields=["actor", "created_at"], name="sadm_admin_actor_created_idx"),
        ),
        migrations.AddIndex(
            model_name="superadminactivitylog",
            index=models.Index(fields=["target_user", "created_at"], name="sadm_admin_target_created_idx"),
        ),
        migrations.AddIndex(
            model_name="superadminactivitylog",
            index=models.Index(fields=["action", "created_at"], name="sadm_admin_action_created_idx"),
        ),
    ]
