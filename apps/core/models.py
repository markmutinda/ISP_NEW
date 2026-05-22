"""
Core models for ISP Management System
"""
import uuid
import json
from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.utils import timezone
from django.core.validators import RegexValidator
from django.conf import settings
from django_tenants.models import DomainMixin, TenantMixin


class Domain(DomainMixin):
    """Domain model for tenant-specific domains"""
    
    class Meta:
        app_label = 'core'


class AuditMixin(models.Model):
    """Mixin to add audit fields to models"""
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_%(class)s"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_%(class)s"
    )

    class Meta:
        abstract = True
        app_label = 'core'


class UserManager(BaseUserManager):
    """Custom user manager for handling user creation"""
    
    def create_user(self, email=None, password=None, **extra_fields):
        """Create and save a regular User with the given email and password.
        
        Email is OPTIONAL for customer accounts — many Kenyan ISP subscribers
        don't have email addresses. When email is empty/None, the user is
        identified by phone_number instead.
        """
        # FIX: Keep None as None to prevent unique constraint violations on empty strings
        if email and email.strip():
            email = self.normalize_email(email.strip())
        else:
            email = None

        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """Create and save a SuperUser with the given email and password."""
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        extra_fields.setdefault('role', 'admin')

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        
        return self.create_user(email, password, **extra_fields)


class User(AbstractUser, AuditMixin):
    """Custom User model with additional fields for ISP management"""
    
    USER_ROLES = (
        ('admin', 'Administrator'),
        ('staff', 'Staff Member'),
        ('technician', 'Technician'),
        ('customer', 'Customer'),
        ('accountant', 'Accountant'),
        ('support', 'Support Agent'),
    )
    
    GENDER_CHOICES = (
        ('M', 'Male'),
        ('F', 'Female'),
        ('O', 'Other'),
    )
    
    # Remove username field, use email instead
    username = None
    # FIX: Add null=True, blank=True to allow NULL values in database
    email = models.EmailField('Email Address', unique=True, null=True, blank=True)
    
    # Additional fields
    phone_regex = RegexValidator(
        regex=r'^\+?1?\d{9,15}$',
        message="Phone number must be entered in the format: '+254712345678'. Up to 15 digits allowed."
    )
    phone_number = models.CharField(
        validators=[phone_regex],
        max_length=17,
        unique=True,
        verbose_name='Phone Number'
    )
    id_number = models.CharField(
        max_length=20,
        unique=True,
        null=True,
        blank=True,
        verbose_name='National ID/Passport'
    )
    gender = models.CharField(
        max_length=1,
        choices=GENDER_CHOICES,
        null=True,
        blank=True
    )
    date_of_birth = models.DateField(null=True, blank=True)
    profile_picture = models.ImageField(
        upload_to='profile_pictures/',
        null=True,
        blank=True,
        verbose_name='Profile Picture'
    )
    
    # Role and permissions
    role = models.CharField(
        max_length=20,
        choices=USER_ROLES,
        default='customer',
        verbose_name='User Role'
    )
    
    company = models.ForeignKey(
        'Company',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employees',
        verbose_name='ISP Company'
    )
    
    # Tenant relationship - make it nullable
    tenant = models.ForeignKey(
        'Tenant',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='users',
        verbose_name='Tenant'
    )

    company_name = models.CharField(max_length=255, blank=True, null=True)
    tenant_subdomain = models.CharField(max_length=100, blank=True, null=True)
    is_verified = models.BooleanField(default=False)
    verification_token = models.UUIDField(default=uuid.uuid4, editable=False)
    verification_token_expiry = models.DateTimeField(null=True, blank=True)
    
    # Set email as the username field
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['phone_number', 'first_name', 'last_name']
    
    objects = UserManager()
    
    class Meta:
        app_label = 'core'
        ordering = ['-created_at']
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        
    def __str__(self):
        return f"{self.get_full_name()} ({self.email or 'No Email'})"
    
    def get_full_name(self):
        """Return the full name of the user"""
        return f"{self.first_name} {self.last_name}".strip()
    
    def get_role_display_name(self):
        """Return the human-readable role name"""
        return dict(self.USER_ROLES).get(self.role, self.role)
    
    def save(self, *args, **kwargs):
        # Set denormalized fields from foreign keys if available
        if self.company and not self.company_name:
            self.company_name = self.company.name
        
        if self.tenant and not self.tenant_subdomain:
            self.tenant_subdomain = self.tenant.subdomain
        
        super().save(*args, **kwargs)

    @property
    def is_admin(self):
        return self.role == 'admin' or self.is_superuser
    
    @property
    def is_staff_member(self):
        return self.role in ['admin', 'staff', 'accountant', 'support']
    
    @property
    def is_technician(self):
        return self.role == 'technician'
    
    @property
    def is_customer(self):
        return self.role == 'customer'
    
    @property
    def is_company_admin(self):
        """Check if user is admin of their company"""
        return self.role == 'admin' or self.is_superuser
    
    @property
    def is_company_staff(self):
        """Check if user is staff of their company"""
        return self.role in ['admin', 'staff', 'accountant', 'support', 'technician']


class BaseModel(models.Model):
    """Base model with common fields"""
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        abstract = True
        app_label = 'core'
        ordering = ['-created_at']


class Company(BaseModel):
    """ISP Company model for multi-tenancy support"""
    
    COMPANY_TYPES = (
        ('isp', 'Internet Service Provider'),
        ('corporate', 'Corporate Client'),
        ('reseller', 'Reseller'),
    )
    
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True)
    company_type = models.CharField(max_length=20, choices=COMPANY_TYPES, default='isp')
    
    # Contact Information
    email = models.EmailField()
    phone_number = models.CharField(max_length=20)
    address = models.TextField()
    city = models.CharField(max_length=100)
    county = models.CharField(max_length=100, null=True, blank=True)
    postal_code = models.CharField(max_length=20, null=True, blank=True)
    
    # Business Information
    registration_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        unique=False,
        verbose_name="Registration Number",
        help_text="Optional. Leave blank if not applicable."
    )
    tax_pin = models.CharField(max_length=50, null=True, blank=True)
    website = models.URLField(null=True, blank=True)
    logo = models.ImageField(upload_to='company_logos/', null=True, blank=True)
    
    # Settings
    subscription_plan = models.CharField(max_length=50, default='basic')
    subscription_expiry = models.DateField(null=True, blank=True)
    
    class Meta:
        app_label = 'core'
        ordering = ['name']
        verbose_name_plural = 'Companies'
    
    def __str__(self):
        return self.name
    
    @property
    def total_customers(self):
        return self.customers.count() if hasattr(self, 'customers') else 0
    
    @property
    def active_customers(self):
        return self.customers.filter(is_active=True).count() if hasattr(self, 'customers') else 0


class Tenant(BaseModel, TenantMixin):
    """Tenant model for SaaS multi-tenancy - ONLY model inheriting from TenantMixin"""

    STATUS_CHOICES = (
        ('trial', 'Trial'),
        ('active', 'Active'),
        ('suspended', 'Suspended'),
        ('cancelled', 'Cancelled'),
    )

    company = models.OneToOneField(
        Company,
        on_delete=models.CASCADE,
        related_name='tenant',
        help_text="The ISP/company this tenant belongs to"
    )

    subdomain = models.CharField(
        max_length=100,
        unique=True,
        help_text="Unique subdomain for this ISP (e.g. bluenet)"
    )
    domain = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Custom domain if they have one (optional)"
    )
    database_name = models.CharField(
        max_length=100,
        help_text="Database/schema name prefix or identifier"
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='trial',
        help_text="Current status of this tenant account"
    )

    # Trial & Subscription control
    trial_start = models.DateField(default=timezone.now)
    trial_days = models.PositiveIntegerField(default=2)
    subscription_expiry = models.DateField(null=True, blank=True)

    # Limits & Features
    max_users = models.PositiveIntegerField(default=10)
    max_customers = models.PositiveIntegerField(default=100)
    features = models.JSONField(default=dict)

    # Billing
    billing_cycle = models.CharField(max_length=20, default='monthly')
    monthly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    next_billing_date = models.DateField(null=True, blank=True)

    # Required for django-tenants - TenantMixin requires schema_name
    schema_name = models.SlugField(
        max_length=63,      # Max allowed length in PostgreSQL
        unique=True,        # Must be unique per tenant
        editable=False,     # User should not edit it manually
        default="default_schema"  # Temporary default to satisfy migrations
    )

    class Meta:
        app_label = 'core'
        ordering = ['subdomain']
        verbose_name = "Tenant"
        verbose_name_plural = "Tenants"

    def save(self, *args, **kwargs):
        # Auto-create schema_name from subdomain if not set
        if not self.schema_name or self.schema_name == "default_schema":
            # Create a safe schema name
            schema = self.subdomain.lower().replace('-', '_').replace('.', '_')
            # Remove any non-alphanumeric characters except underscore
            schema = ''.join(c for c in schema if c.isalnum() or c == '_')
            # Ensure it starts with a letter or underscore
            if schema and not schema[0].isalpha() and schema[0] != '_':
                schema = '_' + schema
            self.schema_name = schema[:63]  # Truncate to max length
        
        # Auto-calculate trial subscription fields for new tenants
        if not self.pk and self.status == 'trial':
            self.trial_start = timezone.now().date()
            self.subscription_expiry = self.trial_start + timezone.timedelta(days=self.trial_days)
            self.next_billing_date = self.subscription_expiry

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.company.name} ({self.subdomain}) - {self.status}"

    @property
    def is_trial_expired(self):
        if self.status != 'trial' or not self.subscription_expiry:
            return False
        return timezone.now().date() > self.subscription_expiry

    @property
    def days_left_in_trial(self):
        if self.status != 'trial' or not self.subscription_expiry:
            return 0
        remaining = self.subscription_expiry - timezone.now().date()
        return max(remaining.days, 0)


class SystemSettings(BaseModel):
    """System-wide settings and configurations"""
    
    SETTING_TYPES = (
        ('general', 'General'),
        ('billing', 'Billing'),
        ('network', 'Network'),
        ('email', 'Email'),
        ('sms', 'SMS'),
        ('security', 'Security'),
        ('integration', 'Integration'),
    )
    
    key = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=255)
    value = models.TextField()
    setting_type = models.CharField(max_length=20, choices=SETTING_TYPES, default='general')
    data_type = models.CharField(
        max_length=20,
        choices=(
            ('string', 'String'),
            ('integer', 'Integer'),
            ('float', 'Float'),
            ('boolean', 'Boolean'),
            ('json', 'JSON'),
        ),
        default='string'
    )
    is_public = models.BooleanField(default=False)
    description = models.TextField(null=True, blank=True)
    
    class Meta:
        app_label = 'core'
        ordering = ['setting_type', 'key']
        verbose_name = 'System Setting'
        verbose_name_plural = 'System Settings'
    
    def __str__(self):
        return f"{self.name} ({self.key})"
    
    def get_value(self):
        """Return the value in the correct data type"""
        if self.data_type == 'integer':
            return int(self.value)
        elif self.data_type == 'float':
            return float(self.value)
        elif self.data_type == 'boolean':
            return self.value.lower() in ['true', '1', 'yes']
        elif self.data_type == 'json':
            return json.loads(self.value)
        else:
            return self.value
    
    @classmethod
    def get_setting(cls, key, default=None):
        """Helper method to get a setting value"""
        try:
            setting = cls.objects.get(key=key)
            return setting.get_value()
        except cls.DoesNotExist:
            return default


class AuditLog(BaseModel):
    """Model to track all system changes"""
    
    ACTION_TYPES = (
        ('create', 'Create'),
        ('update', 'Update'),
        ('delete', 'Delete'),
        ('login', 'Login'),
        ('logout', 'Logout'),
        ('view', 'View'),
        ('export', 'Export'),
        ('import', 'Import'),
    )
    
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_logs'
    )
    action = models.CharField(max_length=20, choices=ACTION_TYPES)
    model_name = models.CharField(max_length=100)
    object_id = models.CharField(max_length=100, null=True, blank=True)
    object_repr = models.CharField(max_length=255, null=True, blank=True)
    
    # Changes
    changes = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    
    # Metadata
    timestamp = models.DateTimeField(auto_now_add=True)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_logs'
    )
    
    class Meta:
        app_label = 'core'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['timestamp']),
            models.Index(fields=['user', 'timestamp']),
            models.Index(fields=['model_name', 'object_id']),
        ]
    
    def __str__(self):
        return f"{self.user} {self.action} {self.model_name} {self.object_id}"
    
    @classmethod
    def log_action(cls, user, action, model_name, object_id=None, object_repr=None, 
                   changes=None, ip_address=None, user_agent=None, tenant=None):
        """Helper method to create audit log entries"""
        return cls.objects.create(
            user=user,
            action=action,
            model_name=model_name,
            object_id=object_id,
            object_repr=object_repr,
            changes=changes,
            ip_address=ip_address,
            user_agent=user_agent,
            tenant=tenant
        )


class GlobalSystemSettings(models.Model):
    """Global system settings singleton"""
    
    # RADIUS Settings
    primary_server = models.CharField(max_length=255, blank=True)
    primary_port = models.IntegerField(default=1812)
    primary_secret = models.CharField(max_length=255, blank=True)
    secondary_server = models.CharField(max_length=255, blank=True)
    secondary_port = models.IntegerField(default=1812)
    secondary_secret = models.CharField(max_length=255, blank=True)
    accounting_port = models.IntegerField(default=1813)
    timeout = models.IntegerField(default=5)
    retries = models.IntegerField(default=3)
   
    # Automation Settings
    auto_renew = models.BooleanField(default=True)
    auto_expiry = models.BooleanField(default=True)
    auto_notifications = models.BooleanField(default=True)
    auto_backup = models.BooleanField(default=False)
    auto_reports = models.BooleanField(default=True)
    grace_period = models.IntegerField(default=3)
    backup_frequency = models.CharField(
        max_length=20, 
        default='daily', 
        choices=[('daily', 'Daily'), ('weekly', 'Weekly'), ('monthly', 'Monthly')]
    )
    report_frequency = models.CharField(
        max_length=20, 
        default='weekly', 
        choices=[('daily', 'Daily'), ('weekly', 'Weekly'), ('monthly', 'Monthly')]
    )
   
    # Notification Settings
    email_enabled = models.BooleanField(default=True)
    sms_enabled = models.BooleanField(default=True)
    payment_notifications = models.BooleanField(default=True)
    expiry_notifications = models.BooleanField(default=True)
    system_alerts = models.BooleanField(default=True)
    marketing_emails = models.BooleanField(default=False)
    admin_email = models.EmailField(blank=True)
    sms_gateway = models.CharField(max_length=50, default='africastalking')

    # Security Settings
    # Controls whether tenant admin login requires email OTP challenge.
    admin_email_otp_enabled = models.BooleanField(default=False)

    class Meta:
        app_label = 'core'
        verbose_name = 'Global System Settings'
        verbose_name_plural = 'Global System Settings'

    def __str__(self):
        return "Global System Settings"

    @classmethod
    def get_solo(cls):
        """Get or create the singleton instance"""
        obj, created = cls.objects.get_or_create(pk=1)
        return obj


class GlobalRouterMap(models.Model):
    """
    CENTRAL RADIUS PHONEBOOK
    This model lives in the 'public' schema. 
    It tells the central RADIUS server which tenant schema owns which incoming router IP.
    """
    nas_ip = models.GenericIPAddressField(
        unique=True, 
        db_index=True,
        help_text="The VPN IP of the router (e.g., 10.8.0.2)"
    )
    nas_secret = models.CharField(
        max_length=255,
        help_text="The RADIUS shared secret for this router"
    )
    tenant = models.ForeignKey(
        'core.Tenant', 
        on_delete=models.CASCADE,
        related_name='mapped_routers'
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'core'
        verbose_name = 'Global Router Map'
        verbose_name_plural = 'Global Router Maps'

    def __str__(self):
        return f"{self.nas_ip} -> {self.tenant.schema_name}"

    @property
    def schema_name(self):
        """Helper for FreeRADIUS dynamic queries"""
        return self.tenant.schema_name


class Changelog(models.Model):
    """
    Platform-wide updates, features, and bug fixes written by Superadmin
    and broadcasted to all ISP tenant dashboards.
    """
    TYPE_CHOICES = (
        ('feature', 'New Feature'),
        ('improvement', 'Improvement'),
        ('bugfix', 'Bug Fix'),
        ('maintenance', 'Maintenance'),
    )
    
    title = models.CharField(max_length=255)
    version = models.CharField(max_length=50, blank=True, null=True, help_text="e.g., v1.2.0")
    content = models.TextField(help_text="Markdown or HTML content of the update")
    update_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='feature')
    
    is_published = models.BooleanField(default=True, help_text="Uncheck to hide from ISPs as a draft")
    release_date = models.DateField(auto_now_add=True)
    notification_channels = models.JSONField(default=list, blank=True)
    notification_sent_at = models.DateTimeField(null=True, blank=True)
    notification_summary = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-release_date', '-created_at']
        verbose_name = 'Changelog'
        verbose_name_plural = 'Changelogs'

    def __str__(self):
        return f"{self.version} - {self.title}" if self.version else self.title


class FeatureRequest(models.Model):
    """
    Feature requests submitted by ISPs to request new functionality or improvements.
    Lives in the public schema so all ISPs can see and vote on each other's requests.
    """
    STATUS_CHOICES = (
        ('pending', 'Pending Review'),
        ('planned', 'Planned'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('rejected', 'Rejected'),
    )
    
    CATEGORY_CHOICES = (
        ('network', 'Network & Mikrotik'),
        ('billing', 'Billing & Payments'),
        ('hotspot', 'Hotspot & Vouchers'),
        ('ui_ux', 'Dashboard & UI'),
        ('automation', 'Automation'),
        ('other', 'Other'),
    )

    title = models.CharField(max_length=255)
    description = models.TextField()
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='other')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    
    # Track which ISP created it
    requested_by_tenant = models.ForeignKey('core.Tenant', on_delete=models.CASCADE, related_name='feature_requests')
    
    # Official response from Netily
    admin_comment = models.TextField(blank=True, null=True)
    
    # Quick count for sorting
    upvotes_count = models.PositiveIntegerField(default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'core'
        ordering = ['-upvotes_count', '-created_at']

    def __str__(self):
        return self.title


class FeatureUpvote(models.Model):
    """
    Tracks which ISP has upvoted which feature request.
    Ensures one ISP = one vote per feature request.
    """
    feature_request = models.ForeignKey(FeatureRequest, on_delete=models.CASCADE, related_name='upvotes')
    tenant = models.ForeignKey('core.Tenant', on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'core'
        # Crucial: This prevents an ISP from upvoting the same feature twice
        unique_together = ('feature_request', 'tenant')

    def __str__(self):
        return f"{self.tenant.company.name} upvoted {self.feature_request.title}"


class RouterTenantIndex(models.Model):
    """
    Public-schema index to resolve router -> tenant in O(1).
    Used to avoid cross-tenant loops for auth_key or ID lookups.

    IMPORTANT:
    - router_auth_key is globally unique and is the preferred lookup key.
    - router_id is tenant-local and may repeat across schemas.
    - tenant_schema + router_id is unique.
    """
    # router_auth_key: Primary lookup key, globally unique
    router_auth_key = models.CharField(max_length=64, unique=True, db_index=True)

    # Router.id comes from Django's DEFAULT_AUTO_FIELD / BigAutoField.
    # It is NOT globally unique because each tenant schema has its own sequence.
    router_id = models.BigIntegerField(db_index=True)

    tenant = models.ForeignKey(
        'core.Tenant',
        on_delete=models.CASCADE,
        related_name='router_tenant_indexes',
    )
    tenant_schema = models.CharField(max_length=63, db_index=True)
    router_name = models.CharField(max_length=255, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'core'
        verbose_name = 'Router Tenant Index'
        verbose_name_plural = 'Router Tenant Indexes'
        indexes = [
            models.Index(fields=['tenant_schema']),
            models.Index(fields=['is_active']),
            models.Index(fields=['router_id']),
            models.Index(fields=['tenant_schema', 'router_id']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['tenant_schema', 'router_id'],
                name='uniq_routertenantindex_schema_router_id',
            ),
        ]

    def __str__(self):
        return f"{self.router_auth_key} -> {self.tenant_schema}:{self.router_id}"


class TumaCallbackMap(models.Model):
    """
    Shared/public lookup: maps Tuma request IDs to tenant schema.
    This model must live in a SHARED app (core) so it's accessible in public schema.
    """
    merchant_request_id = models.CharField(max_length=120, db_index=True, unique=True)
    checkout_request_id = models.CharField(max_length=120, db_index=True, unique=True)
    schema_name = models.CharField(max_length=63, db_index=True)
    payment_reference = models.CharField(max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "core"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.schema_name} | {self.checkout_request_id}"


class Lead(models.Model):
    """
    Stores leads from the public landing page.
    Lives in the public schema.
    """
    name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True, default="")
    company_name = models.CharField(max_length=200, blank=True, default="")
    lead_source = models.CharField(max_length=120, blank=True, default="")
    message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "core"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.email})"


class EmailOTP(models.Model):
    PURPOSE_LOGIN = "login"
    PURPOSE_PAYMENT_METHOD_CHANGE = "payment_method_change"
    PURPOSE_CHOICES = (
        (PURPOSE_LOGIN, "Tenant Login"),
        (PURPOSE_PAYMENT_METHOD_CHANGE, "Payment Method Verification"),
    )

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="email_otps")
    login_challenge = models.ForeignKey(
        "core.LoginOTPChallenge",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="otps",
    )
    purpose = models.CharField(max_length=40, choices=PURPOSE_CHOICES, default=PURPOSE_LOGIN, db_index=True)
    code = models.CharField(max_length=6)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(db_index=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        app_label = "core"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "purpose", "created_at"]),
            models.Index(fields=["expires_at", "is_used"]),
        ]

    def __str__(self):
        return f"OTP<{self.user_id}:{self.purpose}:{self.created_at}>"


class LoginOTPChallenge(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="login_otp_challenges")
    tenant_scope = models.CharField(max_length=120, blank=True, default="")
    session_scope = models.CharField(max_length=255, blank=True, default="")
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    resend_count = models.PositiveSmallIntegerField(default=0)
    is_completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(db_index=True)
    last_sent_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        app_label = "core"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["tenant_scope", "session_scope"]),
            models.Index(fields=["expires_at", "is_completed"]),
        ]

    def __str__(self):
        return f"LoginChallenge<{self.user_id}:{self.id}>"


# ─────────────────────────────────────────────────────────────────────────────
# LEAD TELEGRAM ALERT SIGNAL
# ─────────────────────────────────────────────────────────────────────────────

from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=Lead)
def notify_admins_of_new_lead(sender, instance, created, **kwargs):
    """
    Send a Telegram alert to superadmins whenever a new lead is created via the public landing page.
    This runs asynchronously in a Celery task so the form submission doesn't wait for Telegram.
    """
    if created:  # Only trigger when a NEW lead is created, not updated
        from apps.notifications.tasks import send_telegram_lead_alert
        
        # Send to the celery queue (async)
        send_telegram_lead_alert.delay(
            name=instance.name,
            email=instance.email,
            phone=instance.phone or 'N/A',
            company=instance.company_name or 'Not specified'
        )
