import uuid

from django.conf import settings
from django.db import models


class TenantDeletionJob(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = (
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    )

    STEP_QUEUED = "queued"
    STEP_REVOKING_ACCESS = "revoking_access"
    STEP_CLEANING_STORAGE = "cleaning_storage"
    STEP_CLEANING_INTEGRATIONS = "cleaning_integrations"
    STEP_DROPPING_SCHEMA = "dropping_schema"
    STEP_DELETING_RECORDS = "deleting_records"
    STEP_COMPLETED = "completed"
    STEP_FAILED = "failed"

    STEP_CHOICES = (
        (STEP_QUEUED, "Queued"),
        (STEP_REVOKING_ACCESS, "Revoking Access"),
        (STEP_CLEANING_STORAGE, "Cleaning Storage"),
        (STEP_CLEANING_INTEGRATIONS, "Cleaning Integrations"),
        (STEP_DROPPING_SCHEMA, "Dropping Schema"),
        (STEP_DELETING_RECORDS, "Deleting Records"),
        (STEP_COMPLETED, "Completed"),
        (STEP_FAILED, "Failed"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "core.Tenant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deletion_jobs",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tenant_deletion_jobs",
    )
    company_name = models.CharField(max_length=255)
    subdomain = models.CharField(max_length=100)
    schema_name = models.CharField(max_length=63, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    current_step = models.CharField(max_length=40, choices=STEP_CHOICES, default=STEP_QUEUED)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    status_message = models.CharField(max_length=255, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    requested_options = models.JSONField(default=dict, blank=True)
    cleanup_summary = models.JSONField(default=dict, blank=True)
    step_history = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["status", "created_at"],
                name="sadm_tdel_status_created_idx",
            ),
            models.Index(
                fields=["tenant", "created_at"],
                name="sadm_tdel_tenant_created_idx",
            ),
            models.Index(
                fields=["schema_name", "created_at"],
                name="sadm_tdel_schema_created_idx",
            ),
        ]

    def __str__(self):
        return f"{self.company_name} ({self.schema_name}) - {self.status}"
