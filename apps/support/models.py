from django.db import models
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator
from django.conf import settings

# Use settings.AUTH_USER_MODEL for User references
User = settings.AUTH_USER_MODEL


class TicketCategory(models.Model):
    """Categories for support tickets"""
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    priority = models.IntegerField(default=5, validators=[MinValueValidator(1), MaxValueValidator(10)])
    sla_hours = models.PositiveIntegerField(default=24, help_text="SLA response time in hours")
    is_active = models.BooleanField(default=True)
    
    # Company association
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='ticket_categories',
        null=True,
        blank=True
    )
    
    class Meta:
        verbose_name_plural = "Ticket Categories"
        ordering = ['priority', 'name']
    
    def __str__(self):
        return self.name


class TicketStatus(models.Model):
    """Ticket status workflow"""
    name = models.CharField(max_length=50)
    description = models.TextField(blank=True)
    is_open = models.BooleanField(default=True)
    is_closed = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    color = models.CharField(max_length=20, default='#007bff')
    
    # Company association
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='ticket_statuses',
        null=True,
        blank=True
    )
    
    class Meta:
        verbose_name_plural = "Ticket Statuses"
        ordering = ['order']
    
    def __str__(self):
        return self.name


class Technician(models.Model):
    """Support technicians/engineers"""
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='technician_profile'
    )
    employee_id = models.CharField(max_length=50, unique=True)
    
    # Technician details
    department = models.CharField(max_length=100, choices=[
        ('support', 'Customer Support'),
        ('technical', 'Technical Support'),
        ('field', 'Field Engineer'),
        ('noc', 'Network Operations'),
        ('billing', 'Billing Support'),
    ])
    expertise = models.JSONField(default=list, help_text="List of expertise areas")
    certification = models.TextField(blank=True)
    
    # Availability
    is_available = models.BooleanField(default=True)
    availability_schedule = models.JSONField(default=dict, help_text="Weekly schedule in JSON format")
    max_active_tickets = models.PositiveIntegerField(default=10)
    
    # Performance metrics
    average_rating = models.FloatField(default=0.0, validators=[MinValueValidator(0), MaxValueValidator(5)])
    total_tickets_resolved = models.PositiveIntegerField(default=0)
    total_resolution_time = models.DurationField(default=timezone.timedelta(0))
    
    # Contact info
    work_phone = models.CharField(max_length=20, blank=True)
    mobile_phone = models.CharField(max_length=20, blank=True)
    emergency_contact = models.TextField(blank=True)
    
    # Company association
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='technicians',
        null=True,
        blank=True
    )
    
    # Metadata
    hire_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['user__first_name', 'user__last_name']
    
    def __str__(self):
        return f"{self.user.get_full_name()} ({self.department})"
    
    @property
    def current_active_tickets(self):
        return self.assigned_tickets.filter(status__is_closed=False).count()
    
    @property
    def average_resolution_time(self):
        if self.total_tickets_resolved > 0:
            total_seconds = self.total_resolution_time.total_seconds()
            return total_seconds / self.total_tickets_resolved
        return 0
    
    @property
    def efficiency_score(self):
        """Calculate technician efficiency score (0-100)"""
        if self.max_active_tickets == 0:
            return 0
        
        # Active tickets ratio (lower is better)
        active_ratio = min(self.current_active_tickets / self.max_active_tickets, 1.0)
        
        # Rating component
        rating_score = self.average_rating / 5.0
        
        # Resolution time component (lower is better)
        avg_resolution_hours = self.average_resolution_time / 3600
        resolution_score = max(0, 1.0 - (avg_resolution_hours / 48))  # 48 hours max
        
        # Calculate overall score
        score = (active_ratio * 0.3 + rating_score * 0.4 + resolution_score * 0.3) * 100
        
        return round(score, 1)


class Ticket(models.Model):
    """Support ticket model"""
    PRIORITY_LEVELS = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('urgent', 'Urgent'),
        ('critical', 'Critical'),
    ]
    
    SOURCE_CHANNELS = [
        ('phone', 'Phone'),
        ('email', 'Email'),
        ('web', 'Web Portal'),
        ('chat', 'Live Chat'),
        ('mobile', 'Mobile App'),
        ('walkin', 'Walk-in'),
        ('auto', 'Auto-generated'),
    ]
    
    # Ticket identification
    ticket_number = models.CharField(max_length=20, unique=True, editable=False)
    subject = models.CharField(max_length=200)
    description = models.TextField()
    
    # Customer information
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.CASCADE,
        related_name='support_tickets'
    )
    contact_person = models.CharField(max_length=100, blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    contact_email = models.EmailField(blank=True)
    
    # Classification
    category = models.ForeignKey(TicketCategory, on_delete=models.SET_NULL, null=True, blank=True)
    priority = models.CharField(max_length=20, choices=PRIORITY_LEVELS, default='medium')
    source_channel = models.CharField(max_length=20, choices=SOURCE_CHANNELS, default='web')
    
    # Device/Service reference
    related_device_id = models.PositiveIntegerField(null=True, blank=True)
    related_device_type = models.CharField(max_length=50, blank=True)
    related_service_id = models.PositiveIntegerField(null=True, blank=True)
    
    # Assignment
    assigned_to = models.ForeignKey(Technician, on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_tickets')
    assigned_at = models.DateTimeField(null=True, blank=True)
    
    # Status tracking
    status = models.ForeignKey(TicketStatus, on_delete=models.SET_NULL, null=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_tickets')
    
    # SLA tracking
    sla_due_at = models.DateTimeField(null=True, blank=True)
    first_response_at = models.DateTimeField(null=True, blank=True)
    first_response_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='first_response_tickets')
    resolution_due_at = models.DateTimeField(null=True, blank=True)
    
    # Resolution
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='resolved_tickets')
    resolution_notes = models.TextField(blank=True)
    resolution_category = models.CharField(max_length=50, blank=True, choices=[
        ('hardware', 'Hardware Issue'),
        ('software', 'Software Issue'),
        ('configuration', 'Configuration'),
        ('billing', 'Billing Issue'),
        ('service', 'Service Outage'),
        ('user_error', 'User Error'),
        ('other', 'Other'),
    ])
    
    # Customer satisfaction
    customer_rating = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)])
    customer_feedback = models.TextField(blank=True)
    
    # Internal tracking
    internal_notes = models.TextField(blank=True)
    is_escalated = models.BooleanField(default=False)
    escalation_level = models.PositiveIntegerField(default=0)
    escalation_reason = models.TextField(blank=True)
    
    # Company association
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='tickets',
        null=True,
        blank=True
    )
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['ticket_number']),
            models.Index(fields=['customer', 'created_at']),
            models.Index(fields=['assigned_to', 'status']),
            models.Index(fields=['priority', 'status']),
        ]
    
    def __str__(self):
        return f"{self.ticket_number}: {self.subject}"
    
    def save(self, *args, **kwargs):
        if not self.ticket_number:
            # Generate ticket number: TKT-YYYYMMDD-XXXXX
            date_str = timezone.now().strftime('%Y%m%d')
            last_ticket = Ticket.objects.filter(
                ticket_number__startswith=f'TKT-{date_str}'
            ).order_by('-ticket_number').first()
            
            if last_ticket:
                last_num = int(last_ticket.ticket_number.split('-')[-1])
                new_num = last_num + 1
            else:
                new_num = 1
            
            self.ticket_number = f"TKT-{date_str}-{new_num:05d}"
        
        # Calculate SLA due dates
        if not self.sla_due_at and self.category and self.category.sla_hours:
            self.sla_due_at = self.created_at + timezone.timedelta(hours=self.category.sla_hours)
        
        super().save(*args, **kwargs)
    
    @property
    def age(self):
        """Ticket age in hours"""
        return (timezone.now() - self.created_at).total_seconds() / 3600
    
    @property
    def is_overdue(self):
        """Check if ticket is overdue"""
        if self.sla_due_at and not self.first_response_at:
            return timezone.now() > self.sla_due_at
        return False
    
    @property
    def time_to_first_response(self):
        """Time to first response in hours"""
        if self.first_response_at:
            return (self.first_response_at - self.created_at).total_seconds() / 3600
        return None
    
    @property
    def time_to_resolution(self):
        """Time to resolution in hours"""
        if self.resolved_at:
            return (self.resolved_at - self.created_at).total_seconds() / 3600
        return None
    
    @property
    def customer_name(self):
        return self.customer.user.get_full_name() if self.customer and self.customer.user else "Unknown"


class TicketMessage(models.Model):
    """Messages/updates within a ticket"""
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='messages')
    
    # Message content
    message = models.TextField()
    is_internal = models.BooleanField(default=False, help_text="Internal note not visible to customer")
    
    # Sender information
    sender = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='sent_messages')
    sender_type = models.CharField(max_length=20, choices=[
        ('customer', 'Customer'),
        ('technician', 'Technician'),
        ('system', 'System'),
        ('admin', 'Admin'),
    ], default='technician')
    
    # Attachments
    attachments = models.JSONField(default=list, help_text="List of attached file URLs")
    
    # Read receipts
    read_by_customer = models.BooleanField(default=False)
    read_by_staff = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    
    # Company association
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='ticket_messages',
        null=True,
        blank=True
    )
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['created_at']
    
    def __str__(self):
        return f"Message #{self.id} for {self.ticket.ticket_number}"


class TicketActivity(models.Model):
    """Audit log for ticket activities"""
    ACTIVITY_TYPES = [
        ('created', 'Ticket Created'),
        ('updated', 'Ticket Updated'),
        ('assigned', 'Ticket Assigned'),
        ('status_changed', 'Status Changed'),
        ('priority_changed', 'Priority Changed'),
        ('escalated', 'Ticket Escalated'),
        ('message_added', 'Message Added'),
        ('resolved', 'Ticket Resolved'),
        ('closed', 'Ticket Closed'),
        ('reopened', 'Ticket Reopened'),
    ]
    
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='activities')
    activity_type = models.CharField(max_length=50, choices=ACTIVITY_TYPES)
    description = models.TextField()
    
    # User who performed the activity
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    
    # Changes made
    changes = models.JSONField(default=dict, help_text="JSON representation of changes")
    
    # Company association
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='ticket_activities',
        null=True,
        blank=True
    )
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = "Ticket Activities"
    
    def __str__(self):
        return f"{self.activity_type} for {self.ticket.ticket_number}"


class KnowledgeBaseArticle(models.Model):
    """Knowledge base articles for self-service"""
    CATEGORIES = [
        ('general', 'General'),
        ('billing', 'Billing & Payments'),
        ('technical', 'Technical Support'),
        ('installation', 'Installation'),
        ('troubleshooting', 'Troubleshooting'),
        ('faq', 'FAQ'),
        ('announcement', 'Announcement'),
    ]
    
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('review', 'Under Review'),
        ('published', 'Published'),
        ('archived', 'Archived'),
    ]
    
    # Article information
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    content = models.TextField()
    excerpt = models.TextField(blank=True, help_text="Short summary for listings")
    
    # Categorization
    category = models.CharField(max_length=50, choices=CATEGORIES, default='general')
    subcategory = models.CharField(max_length=100, blank=True)
    tags = models.JSONField(default=list, help_text="List of tags for search")
    
    # Author and ownership
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='knowledgebase_articles')
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_articles')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    
    # Status and visibility
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    is_featured = models.BooleanField(default=False)
    is_pinned = models.BooleanField(default=False)
    
    # SEO and accessibility
    meta_title = models.CharField(max_length=200, blank=True)
    meta_description = models.TextField(blank=True)
    keywords = models.TextField(blank=True)
    
    # Statistics
    view_count = models.PositiveIntegerField(default=0)
    helpful_yes = models.PositiveIntegerField(default=0)
    helpful_no = models.PositiveIntegerField(default=0)
    last_viewed_at = models.DateTimeField(null=True, blank=True)
    
    # Company association
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='knowledgebase_articles',
        null=True,
        blank=True
    )
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-published_at', '-created_at']
        verbose_name = "Knowledge Base Article"
        verbose_name_plural = "Knowledge Base Articles"
    
    def __str__(self):
        return self.title
    
    def save(self, *args, **kwargs):
        # Set published_at when status changes to published
        if self.status == 'published' and not self.published_at:
            self.published_at = timezone.now()
        
        super().save(*args, **kwargs)
    
    @property
    def helpful_percentage(self):
        """Calculate helpful percentage"""
        total = self.helpful_yes + self.helpful_no
        if total > 0:
            return (self.helpful_yes / total) * 100
        return 0


class FAQ(models.Model):
    """Frequently Asked Questions"""
    question = models.CharField(max_length=500)
    answer = models.TextField()
    
    # Categorization
    category = models.CharField(max_length=100, blank=True)
    subcategory = models.CharField(max_length=100, blank=True)
    tags = models.JSONField(default=list)
    
    # Display order
    display_order = models.PositiveIntegerField(default=0)
    
    # Statistics
    view_count = models.PositiveIntegerField(default=0)
    helpful_yes = models.PositiveIntegerField(default=0)
    helpful_no = models.PositiveIntegerField(default=0)
    
    # Status
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    
    # Company association
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='faqs',
        null=True,
        blank=True
    )
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['display_order', 'category', 'question']
        verbose_name = "FAQ"
        verbose_name_plural = "FAQs"
    
    def __str__(self):
        return self.question
    
    @property
    def helpful_percentage(self):
        """Calculate helpful percentage"""
        total = self.helpful_yes + self.helpful_no
        if total > 0:
            return (self.helpful_yes / total) * 100
        return 0


class ServiceOutage(models.Model):
    """Track service outages"""
    OUTAGE_TYPES = [
        ('planned', 'Planned Maintenance'),
        ('unplanned', 'Unplanned Outage'),
        ('partial', 'Partial Outage'),
        ('full', 'Full Outage'),
    ]
    
    SEVERITY_LEVELS = [
        ('minor', 'Minor'),
        ('moderate', 'Moderate'),
        ('major', 'Major'),
        ('critical', 'Critical'),
    ]
    
    # Outage information
    title = models.CharField(max_length=200)
    description = models.TextField()
    outage_type = models.CharField(max_length=20, choices=OUTAGE_TYPES)
    severity = models.CharField(max_length=20, choices=SEVERITY_LEVELS)
    
    # Affected areas
    affected_areas = models.JSONField(default=list, help_text="List of affected areas or zones")
    affected_customers = models.ManyToManyField('customers.Customer', blank=True)
    estimated_customers_affected = models.PositiveIntegerField(default=0)
    
    # Timing
    start_time = models.DateTimeField()
    estimated_resolution_time = models.DateTimeField(null=True, blank=True)
    actual_resolution_time = models.DateTimeField(null=True, blank=True)
    
    # Status
    status = models.CharField(max_length=20, choices=[
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('resolved', 'Resolved'),
        ('cancelled', 'Cancelled'),
    ], default='pending')
    
    # Updates
    updates = models.JSONField(default=list, help_text="List of status updates")
    
    # Communication
    notify_customers = models.BooleanField(default=True)
    notification_sent = models.BooleanField(default=False)
    
    # Root cause and resolution
    root_cause = models.TextField(blank=True)
    resolution = models.TextField(blank=True)
    preventative_measures = models.TextField(blank=True)
    
    # Company association
    company = models.ForeignKey(
        'core.Company',
        on_delete=models.CASCADE,
        related_name='service_outages',
        null=True,
        blank=True
    )
    
    # Metadata
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-start_time']
        verbose_name = "Service Outage"
        verbose_name_plural = "Service Outages"
    
    def __str__(self):
        return f"{self.title} ({self.get_outage_type_display()})"
    
    @property
    def duration(self):
        """Calculate outage duration"""
        if self.actual_resolution_time:
            return self.actual_resolution_time - self.start_time
        elif self.estimated_resolution_time:
            return self.estimated_resolution_time - self.start_time
        return timezone.now() - self.start_time
    
    @property
    def is_active(self):
        """Check if outage is currently active"""
        return self.status == 'in_progress'