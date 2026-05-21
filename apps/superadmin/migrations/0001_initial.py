from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0012_merge_0009_changelog_notification_fields_0011_alter_routertenantindex_router_id_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="TenantDeletionJob",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("company_name", models.CharField(max_length=255)),
                ("subdomain", models.CharField(max_length=100)),
                ("schema_name", models.CharField(db_index=True, max_length=63)),
                ("status", models.CharField(choices=[("queued", "Queued"), ("running", "Running"), ("completed", "Completed"), ("failed", "Failed")], default="queued", max_length=20)),
                ("current_step", models.CharField(choices=[("queued", "Queued"), ("revoking_access", "Revoking Access"), ("cleaning_storage", "Cleaning Storage"), ("cleaning_integrations", "Cleaning Integrations"), ("dropping_schema", "Dropping Schema"), ("deleting_records", "Deleting Records"), ("completed", "Completed"), ("failed", "Failed")], default="queued", max_length=40)),
                ("progress_percent", models.PositiveSmallIntegerField(default=0)),
                ("status_message", models.CharField(blank=True, default="", max_length=255)),
                ("error_message", models.TextField(blank=True, default="")),
                ("requested_options", models.JSONField(blank=True, default=dict)),
                ("cleanup_summary", models.JSONField(blank=True, default=dict)),
                ("step_history", models.JSONField(blank=True, default=list)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("requested_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="tenant_deletion_jobs", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="deletion_jobs", to="core.tenant")),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["status", "created_at"], name="sadm_tdel_status_created_idx"),
                    models.Index(fields=["tenant", "created_at"], name="sadm_tdel_tenant_created_idx"),
                    models.Index(fields=["schema_name", "created_at"], name="sadm_tdel_schema_created_idx"),
                ],
            },
        ),
    ]
