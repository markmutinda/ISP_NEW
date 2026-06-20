from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("superadmin", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="SupportExecutiveProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(blank=True, default="", max_length=120)),
                ("phone_number", models.CharField(blank=True, default="", max_length=30)),
                ("can_register_tenants", models.BooleanField(default=True)),
                ("can_manage_leads", models.BooleanField(default=True)),
                ("can_view_tenants", models.BooleanField(default=True)),
                ("is_active", models.BooleanField(default=True)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_support_executives",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="platform_support_profile",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["user__first_name", "user__email"],
            },
        ),
        migrations.CreateModel(
            name="SupportActivityLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=80)),
                ("area", models.CharField(blank=True, default="", max_length=80)),
                ("summary", models.CharField(max_length=255)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "support_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="platform_support_activity",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="supportexecutiveprofile",
            index=models.Index(fields=["is_active", "created_at"], name="sadm_support_active_idx"),
        ),
        migrations.AddIndex(
            model_name="supportactivitylog",
            index=models.Index(fields=["support_user", "created_at"], name="sadm_sact_user_created_idx"),
        ),
        migrations.AddIndex(
            model_name="supportactivitylog",
            index=models.Index(fields=["action", "created_at"], name="sadm_sact_action_created_idx"),
        ),
    ]
