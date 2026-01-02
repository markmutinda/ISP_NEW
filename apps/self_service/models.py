from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from apps.customers.models import Customer
from apps.billing.models import Invoice, Payment
from apps.support.models import Ticket

User = get_user_model()


class CustomerSession(models.Model):
    """
    Track customer portal sessions
    """
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='sessions')
    session_key = models.CharField(max_length=255, unique=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    login_time = models.DateTimeField(auto_now_add=True)
    logout_time = models.DateTimeField(null=True, blank=True)
    last_activity = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    
    def __str__(self):
        return f"{self.customer.name} - {self.login_time}"
    
    class Meta:
        ordering = ['-login_time']
        indexes = [
            models.Index(fields=['customer', 'login_time']),
            models.Index(fields=['is_active', 'last_activity']),
        ]


class ServiceRequest(models.Model):
    """
    Customer service requests via self-service portal
    """
    REQUEST_TYPES = [
        ('connection', 'New Connection'),
        ('upgrade', 'Plan Upgrade'),
        ('downgrade', 'Plan Downgrade'),
        ('transfer', 'Location Transfer'),
        ('suspension', 'Service Suspension'),
        ('termination', 'Service Termination'),
        ('billing', 'Billing Issue'),
        ('technical', 'Technical Support'),
        ('other', 'Other'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='service_requests')
    request_type = models.CharField(max_length=50, choices=REQUEST_TYPES)
    subject = models.CharField(max_length=255)
    description = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    priority = models.CharField(max_length=20, default='normal')  # low, normal, high
    
    # Request-specific fields
    current_plan = models.ForeignKey('billing.Plan', on_delete=models.SET_NULL, null=True, blank=True, related_name='current_requests')
    requested_plan = models.ForeignKey('billing.Plan', on_delete=models.SET_NULL, null=True, blank=True, related_name='requested_requests')
    current_location = models.TextField(blank=True)
    requested_location = models.TextField(blank=True)
    
    # Tracking
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    estimated_completion = models.DateField(null=True, blank=True)
    actual_completion = models.DateTimeField(null=True, blank=True)
    
    # Customer communication
    customer_notes = models.TextField(blank=True)
    staff_notes = models.TextField(blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.customer.name} - {self.request_type} - {self.status}"
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer', 'status']),
            models.Index(fields=['request_type', 'status']),
            models.Index(fields=['created_at']),
        ]


class UsageAlert(models.Model):
    """
    Alerts for customer usage thresholds
    """
    ALERT_TYPES = [
        ('data', 'Data Usage'),
        ('billing', 'Billing'),
        ('payment', 'Payment'),
        ('service', 'Service'),
    ]
    
    TRIGGER_TYPES = [
        ('threshold', 'Threshold Reached'),
        ('due_date', 'Due Date Approaching'),
        ('overdue', 'Payment Overdue'),
        ('limit_exceeded', 'Limit Exceeded'),
    ]
    
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='usage_alerts')
    alert_type = models.CharField(max_length=50, choices=ALERT_TYPES)
    trigger_type = models.CharField(max_length=50, choices=TRIGGER_TYPES)
    threshold_value = models.FloatField(null=True, blank=True)
    current_value = models.FloatField(null=True, blank=True)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    is_resolved = models.BooleanField(default=False)
    triggered_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"{self.customer.name} - {self.alert_type} - {self.trigger_type}"
    
    class Meta:
        ordering = ['-triggered_at']
        indexes = [
            models.Index(fields=['customer', 'is_read']),
            models.Index(fields=['alert_type', 'triggered_at']),
        ]