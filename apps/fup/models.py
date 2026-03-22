import uuid
from decimal import Decimal

from django.db import models
from django.utils import timezone


class FUPPolicy(models.Model):
    PERIOD_CHOICES = [
        ('DAILY', 'Daily'),
        ('WEEKLY', 'Weekly'),
        ('MONTHLY', 'Monthly'),
        ('SUBSCRIPTION', 'Subscription'),
    ]

    STATUS_CHOICES = [
        ('DRAFT', 'Draft'),
        ('ACTIVE', 'Active'),
        ('INACTIVE', 'Inactive'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)

    data_limit_gb = models.DecimalField(max_digits=10, decimal_places=2)
    throttle_download_mbps = models.PositiveIntegerField()
    throttle_upload_mbps = models.PositiveIntegerField(default=1)

    reset_period = models.CharField(max_length=20, choices=PERIOD_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')

    auto_enforce = models.BooleanField(default=True)
    notify_on_violation = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        'core.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='created_fup_policies',
    )
    updated_by = models.ForeignKey(
        'core.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='updated_fup_policies',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['is_active']),
            models.Index(fields=['reset_period']),
        ]
        verbose_name = 'FUP Policy'
        verbose_name_plural = 'FUP Policies'

    def __str__(self):
        return self.name

    @property
    def limit_bytes(self) -> int:
        return int(Decimal(self.data_limit_gb) * Decimal(1024 ** 3))


class FUPPolicyPlan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    policy = models.ForeignKey(
        'fup.FUPPolicy',
        on_delete=models.CASCADE,
        related_name='plan_links'
    )
    plan = models.ForeignKey(
        'billing.Plan',
        on_delete=models.CASCADE,
        related_name='fup_policy_links'
    )

    is_active = models.BooleanField(default=True)
    linked_by = models.ForeignKey(
        'core.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='fup_plan_links',
    )
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('policy', 'plan')]
        indexes = [
            models.Index(fields=['is_active']),
        ]
        verbose_name = 'FUP Policy Plan Link'
        verbose_name_plural = 'FUP Policy Plan Links'

    def __str__(self):
        return f'{self.policy.name} -> {self.plan.name}'


class FUPPolicyHotspotPlan(models.Model):
    """Link FUP policies to Hotspot plans for hotspot-specific FUP enforcement"""
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    policy = models.ForeignKey(
        'fup.FUPPolicy',
        on_delete=models.CASCADE,
        related_name='hotspot_plan_links'
    )
    hotspot_plan = models.ForeignKey(
        'billing.HotspotPlan',
        on_delete=models.CASCADE,
        related_name='fup_policy_links'
    )
    is_active = models.BooleanField(default=True)
    linked_by = models.ForeignKey(
        'core.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='fup_hotspot_plan_links',
    )
    linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('policy', 'hotspot_plan')]
        indexes = [
            models.Index(fields=['is_active']),
        ]
        verbose_name = 'FUP Policy Hotspot Plan Link'
        verbose_name_plural = 'FUP Policy Hotspot Plan Links'

    def __str__(self):
        return f'{self.policy.name} -> {self.hotspot_plan.name}'


class FUPUsageWindow(models.Model):
    STATUS_CHOICES = [
        ('NORMAL', 'Normal'),
        ('WARNING', 'Warning'),
        ('VIOLATED', 'Violated'),
        ('THROTTLED', 'Throttled'),
        ('RESET', 'Reset'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    policy = models.ForeignKey(
        'fup.FUPPolicy',
        on_delete=models.CASCADE,
        related_name='usage_windows'
    )
    plan = models.ForeignKey(
        'billing.Plan',
        on_delete=models.PROTECT,
        related_name='fup_usage_windows'
    )
    service_connection = models.ForeignKey(
        'customers.ServiceConnection',
        on_delete=models.CASCADE,
        related_name='fup_usage_windows'
    )
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.CASCADE,
        related_name='fup_usage_windows'
    )

    period_start = models.DateTimeField()
    period_end = models.DateTimeField()

    download_bytes = models.BigIntegerField(default=0)
    upload_bytes = models.BigIntegerField(default=0)
    total_bytes = models.BigIntegerField(default=0)
    limit_bytes = models.BigIntegerField()

    usage_percent = models.DecimalField(max_digits=7, decimal_places=2, default=0)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='NORMAL')
    first_exceeded_at = models.DateTimeField(null=True, blank=True)
    last_accounting_update_at = models.DateTimeField(null=True, blank=True)

    is_throttled = models.BooleanField(default=False)
    throttled_at = models.DateTimeField(null=True, blank=True)
    unthrottled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('policy', 'service_connection', 'period_start', 'period_end')]
        ordering = ['-period_start']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['is_throttled']),
            models.Index(fields=['period_start', 'period_end']),
        ]
        verbose_name = 'FUP Usage Window'
        verbose_name_plural = 'FUP Usage Windows'

    def __str__(self):
        return f'{self.customer.customer_code} - {self.policy.name}'

    @property
    def total_gb(self):
        return round(self.total_bytes / (1024 ** 3), 2)

    @property
    def exceeded(self):
        return self.total_bytes > self.limit_bytes


class FUPViolation(models.Model):
    ACTION_CHOICES = [
        ('WARNED', 'Warned'),
        ('THROTTLED', 'Throttled'),
        ('RETHROTTLED', 'Re-throttled'),
        ('RELEASED', 'Released'),
        ('RESET', 'Reset'),
    ]

    STATUS_CHOICES = [
        ('OPEN', 'Open'),
        ('ACKNOWLEDGED', 'Acknowledged'),
        ('RESOLVED', 'Resolved'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    policy = models.ForeignKey(
        'fup.FUPPolicy',
        on_delete=models.CASCADE,
        related_name='violations'
    )
    plan = models.ForeignKey(
        'billing.Plan',
        on_delete=models.PROTECT,
        related_name='fup_violations'
    )
    service_connection = models.ForeignKey(
        'customers.ServiceConnection',
        on_delete=models.CASCADE,
        related_name='fup_violations'
    )
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.CASCADE,
        related_name='fup_violations'
    )
    usage_window = models.ForeignKey(
        'fup.FUPUsageWindow',
        on_delete=models.CASCADE,
        related_name='violations'
    )

    total_usage_bytes = models.BigIntegerField()
    limit_bytes = models.BigIntegerField()
    exceeded_by_bytes = models.BigIntegerField()

    action_taken = models.CharField(max_length=30, choices=ACTION_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='OPEN')
    notes = models.TextField(blank=True)

    occurred_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-occurred_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['occurred_at']),
            models.Index(fields=['action_taken']),
        ]
        verbose_name = 'FUP Violation'
        verbose_name_plural = 'FUP Violations'

    def __str__(self):
        return f'{self.customer.customer_code} - {self.policy.name} - {self.action_taken}'


class FUPThrottleState(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    policy = models.ForeignKey(
        'fup.FUPPolicy',
        on_delete=models.CASCADE,
        related_name='throttle_states'
    )
    service_connection = models.OneToOneField(
        'customers.ServiceConnection',
        on_delete=models.CASCADE,
        related_name='fup_throttle_state'
    )
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.CASCADE,
        related_name='fup_throttle_states'
    )

    original_download_mbps = models.PositiveIntegerField()
    original_upload_mbps = models.PositiveIntegerField()
    throttled_download_mbps = models.PositiveIntegerField()
    throttled_upload_mbps = models.PositiveIntegerField()

    active = models.BooleanField(default=True)
    reason = models.CharField(max_length=255, blank=True)

    applied_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-applied_at']
        indexes = [
            models.Index(fields=['active']),
            models.Index(fields=['applied_at']),
        ]
        verbose_name = 'FUP Throttle State'
        verbose_name_plural = 'FUP Throttle States'

    def __str__(self):
        return f'{self.customer.customer_code} throttled={self.active}'


class FUPAuditLog(models.Model):
    EVENT_CHOICES = [
        ('POLICY_CREATED', 'Policy Created'),
        ('POLICY_UPDATED', 'Policy Updated'),
        ('PLAN_LINKED', 'Plan Linked'),
        ('PLAN_UNLINKED', 'Plan Unlinked'),
        ('HOTSPOT_PLAN_LINKED', 'Hotspot Plan Linked'),
        ('HOTSPOT_PLAN_UNLINKED', 'Hotspot Plan Unlinked'),
        ('USER_THROTTLED', 'User Throttled'),
        ('USER_RELEASED', 'User Released'),
        ('WINDOW_RESET', 'Window Reset'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    policy = models.ForeignKey(
        'fup.FUPPolicy',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='audit_logs'
    )
    service_connection = models.ForeignKey(
        'customers.ServiceConnection',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='fup_audit_logs'
    )
    customer = models.ForeignKey(
        'customers.Customer',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='fup_audit_logs'
    )
    event_type = models.CharField(max_length=40, choices=EVENT_CHOICES)
    message = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        'core.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'FUP Audit Log'
        verbose_name_plural = 'FUP Audit Logs'