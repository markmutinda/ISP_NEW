# apps/billing/models/subscription_models.py
from django.db import models
from django.utils import timezone

class Subscription(models.Model):
    STATUS_CHOICES = [
        ('ACTIVE', 'Active'),
        ('EXPIRED', 'Expired'),
        ('CANCELLED', 'Cancelled'),
    ]
    
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.CASCADE,
        related_name='subscriptions'
    )
    service_connection = models.ForeignKey(
        'customers.ServiceConnection',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='subscriptions'
    )
    plan = models.ForeignKey(
        'billing.Plan',
        on_delete=models.SET_NULL,
        null=True,
        related_name='subscriptions'
    )
    payment = models.ForeignKey(
        'billing.Payment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='subscriptions'
    )
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ACTIVE')
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    schema_name = models.SlugField(max_length=63, editable=False, default='default_schema')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', 'status']),
            models.Index(fields=['expires_at', 'status']),
            models.Index(fields=['schema_name', 'status']),
        ]

    def __str__(self):
        return f"{self.customer.customer_code} - {self.plan.name if self.plan else 'No Plan'} ({self.status})"