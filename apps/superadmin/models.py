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


class PlatformExpenditure(models.Model):
    CATEGORY_INFRASTRUCTURE = "infrastructure"
    CATEGORY_SMS = "sms"
    CATEGORY_PAYROLL = "payroll"
    CATEGORY_MARKETING = "marketing"
    CATEGORY_SOFTWARE = "software"
    CATEGORY_OPERATIONS = "operations"
    CATEGORY_TAX = "tax"
    CATEGORY_OTHER = "other"

    CATEGORY_CHOICES = (
        (CATEGORY_INFRASTRUCTURE, "Infrastructure"),
        (CATEGORY_SMS, "SMS Costs"),
        (CATEGORY_PAYROLL, "Payroll"),
        (CATEGORY_MARKETING, "Marketing"),
        (CATEGORY_SOFTWARE, "Software"),
        (CATEGORY_OPERATIONS, "Operations"),
        (CATEGORY_TAX, "Tax"),
        (CATEGORY_OTHER, "Other"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.CharField(max_length=40, choices=CATEGORY_CHOICES, default=CATEGORY_OPERATIONS)
    title = models.CharField(max_length=160)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=8, default="KES")
    incurred_on = models.DateField(db_index=True)
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="platform_expenditures_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-incurred_on", "-created_at"]
        indexes = [
            models.Index(fields=["category", "incurred_on"], name="sadm_exp_cat_date_idx"),
            models.Index(fields=["incurred_on"], name="sadm_exp_date_idx"),
        ]

    def __str__(self):
        return f"{self.title} - {self.currency} {self.amount}"


class SupportExecutiveProfile(models.Model):
    """Platform-owned support identity attached to a normal User account."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="platform_support_profile",
    )
    title = models.CharField(max_length=120, blank=True, default="")
    phone_number = models.CharField(max_length=30, blank=True, default="")
    can_register_tenants = models.BooleanField(default=True)
    can_manage_leads = models.BooleanField(default=True)
    can_view_tenants = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_support_executives",
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__first_name", "user__email"]
        indexes = [
            models.Index(fields=["is_active", "created_at"], name="sadm_support_active_idx"),
        ]

    def __str__(self):
        return self.user.email or str(self.user_id)


class SupportActivityLog(models.Model):
    """Simple immutable trail for platform support work."""

    support_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="platform_support_activity",
    )
    action = models.CharField(max_length=80)
    area = models.CharField(max_length=80, blank=True, default="")
    summary = models.CharField(max_length=255)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["support_user", "created_at"], name="sadm_sact_user_created_idx"),
            models.Index(fields=["action", "created_at"], name="sadm_sact_action_created_idx"),
        ]

    def __str__(self):
        actor = getattr(self.support_user, "email", None) or "system"
        return f"{actor}: {self.action}"


class SuperAdminActivityLog(models.Model):
    """Immutable audit trail for platform-owner credential and access actions."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="superadmin_actions_performed",
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="superadmin_actions_received",
    )
    action = models.CharField(max_length=80)
    summary = models.CharField(max_length=255)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["actor", "created_at"], name="sadm_admin_actor_created_idx"),
            models.Index(fields=["target_user", "created_at"], name="sadm_admin_target_created_idx"),
            models.Index(fields=["action", "created_at"], name="sadm_admin_action_created_idx"),
        ]

    def __str__(self):
        actor = getattr(self.actor, "email", None) or "system"
        return f"{actor}: {self.action}"
