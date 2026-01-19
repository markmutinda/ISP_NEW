from django.db import models
from django.conf import settings
from django.utils import timezone


# Use settings.AUTH_USER_MODEL for User references
User = settings.AUTH_USER_MODEL


class SupportTicket(models.Model):
    """Support Ticket model matching frontend requirements"""
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('in_progress', 'In Progress'),
        ('pending', 'Pending'),
        ('resolved', 'Resolved'),
        ('closed', 'Closed'),
    ]
    
    PRIORITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('urgent', 'Urgent'),
    ]
    
    CATEGORY_CHOICES = [
        ('technical', 'Technical'),
        ('billing', 'Billing'),
        ('account', 'Account'),
        ('service', 'Service'),
        ('other', 'Other'),
    ]
    
    # Ticket identification
    ticket_number = models.CharField(max_length=20, unique=True, editable=False)
    subject = models.CharField(max_length=255)
    description = models.TextField()
    
    # Status and classification
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='medium')
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='technical')
    
    # Relationships
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.CASCADE,
        related_name='tickets'
    )
    assigned_to = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_tickets'
    )
    
    # SLA tracking
    sla_breached = models.BooleanField(default=False)
    first_response_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        app_label = 'support'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['ticket_number']),
            models.Index(fields=['status']),
            models.Index(fields=['priority']),
            models.Index(fields=['category']),
            models.Index(fields=['customer', 'created_at']),
        ]
    
    def __str__(self):
        return f"{self.ticket_number}: {self.subject}"
    
    def save(self, *args, **kwargs):
        if not self.ticket_number:
            last = SupportTicket.objects.order_by('-id').first()
            num = (last.id + 1) if last else 1
            self.ticket_number = f'TKT-{1000 + num}'
        
        # Auto-update timestamps based on status
        if self.status == 'resolved' and not self.resolved_at:
            self.resolved_at = timezone.now()
        
        super().save(*args, **kwargs)
    
    @property
    def customer_name(self):
        return self.customer.user.get_full_name() if self.customer and self.customer.user else "Unknown"
    
    @property
    def customer_email(self):
        return self.customer.user.email if self.customer and self.customer.user else ""
    
    @property
    def customer_phone(self):
        return self.customer.user.phone_number if self.customer and self.customer.user else ""
    
    @property
    def assigned_to_name(self):
        return self.assigned_to.get_full_name() if self.assigned_to else None


class SupportTicketMessage(models.Model):
    """Support Ticket Message model matching frontend requirements"""
    SENDER_TYPE_CHOICES = [
        ('customer', 'Customer'),
        ('agent', 'Agent'),
        ('system', 'System'),
    ]
    
    ticket = models.ForeignKey(
        SupportTicket,
        on_delete=models.CASCADE,
        related_name='messages'
    )
    sender_type = models.CharField(max_length=20, choices=SENDER_TYPE_CHOICES)
    sender = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True
    )
    message = models.TextField()
    is_internal = models.BooleanField(default=False)
    attachments = models.JSONField(default=list)
    

    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        app_label = 'support'
        ordering = ['created_at']
    
    def __str__(self):
        return f"Message {self.id} for {self.ticket.ticket_number}"
    
    @property
    def sender_name(self):
        return self.sender.get_full_name() if self.sender else "System"
