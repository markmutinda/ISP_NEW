"""
Core models for ISP Management System
"""
import uuid
from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.utils import timezone
from django.core.validators import RegexValidator
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver


class AuditMixin(models.Model):
    """
    Mixin to add audit fields to models
    """
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

class UserManager(BaseUserManager):
    """Custom user manager for handling user creation"""
    
    def create_user(self, email, password=None, **extra_fields):
        """Create and save a regular User with the given email and password."""
        if not email:
            raise ValueError('The Email field must be set')
        email = self.normalize_email(email)
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
    email = models.EmailField('Email Address', unique=True)
    
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
    is_verified = models.BooleanField(default=False)
    verification_token = models.UUIDField(default=uuid.uuid4, editable=False)
    verification_token_expiry = models.DateTimeField(null=True, blank=True)
    
    # Remove the duplicate fields from AuditMixin
    # created_at, updated_at, created_by are already in AuditMixin
    
    # Set email as the username field
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['phone_number', 'first_name', 'last_name']
    
    objects = UserManager()
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        
    def __str__(self):
        return f"{self.get_full_name()} ({self.email})"
    
    def get_full_name(self):
        """Return the full name of the user"""
        return f"{self.first_name} {self.last_name}".strip()
    
    def get_role_display_name(self):
        """Return the human-readable role name"""
        return dict(self.USER_ROLES).get(self.role, self.role)
    
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


# Create a simplified BaseModel to avoid import issues
class BaseModel(models.Model):
    """
    Base model with common fields
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        abstract = True
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
    registration_number = models.CharField(max_length=100, unique=True)
    tax_pin = models.CharField(max_length=50, null=True, blank=True)
    website = models.URLField(null=True, blank=True)
    logo = models.ImageField(upload_to='company_logos/', null=True, blank=True)
    
    # Settings
    subscription_plan = models.CharField(max_length=50, default='basic')
    subscription_expiry = models.DateField(null=True, blank=True)
    
    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Companies'
    
    def __str__(self):
        return self.name
    
    @property
    def total_customers(self):
        return self.customers.count()
    
    @property
    def active_customers(self):
        return self.customers.filter(is_active=True).count()


class Tenant(BaseModel):
    """Tenant model for SaaS functionality"""
    
    STATUS_CHOICES = (
        ('active', 'Active'),
        ('suspended', 'Suspended'),
        ('trial', 'Trial'),
        ('cancelled', 'Cancelled'),
    )
    
    company = models.OneToOneField(
        Company,
        on_delete=models.CASCADE,
        related_name='tenant'
    )
    subdomain = models.CharField(max_length=100, unique=True)
    domain = models.CharField(max_length=255, null=True, blank=True)
    database_name = models.CharField(max_length=100)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='trial')
    
    # Subscription details
    max_users = models.IntegerField(default=10)
    max_customers = models.IntegerField(default=100)
    features = models.JSONField(default=dict)  # Store enabled features
    
    # Billing information
    billing_cycle = models.CharField(max_length=20, default='monthly')
    monthly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    next_billing_date = models.DateField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.company.name} ({self.subdomain})"
    
    @property
    def is_trial_expired(self):
        if self.status == 'trial' and self.created_at:
            trial_end = self.created_at + timezone.timedelta(days=14)
            return timezone.now() > trial_end
        return False


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
            import json
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
    backup_frequency = models.CharField(max_length=20, default='daily', choices=[
        ('daily', 'Daily'), ('weekly', 'Weekly'), ('monthly', 'Monthly')
    ])
    report_frequency = models.CharField(max_length=20, default='weekly', choices=[
        ('daily', 'Daily'), ('weekly', 'Weekly'), ('monthly', 'Monthly')
    ])
   
    # Notification Settings
    email_enabled = models.BooleanField(default=True)
    sms_enabled = models.BooleanField(default=True)
    payment_notifications = models.BooleanField(default=True)
    expiry_notifications = models.BooleanField(default=True)
    system_alerts = models.BooleanField(default=True)
    marketing_emails = models.BooleanField(default=False)
    admin_email = models.EmailField(blank=True)
    sms_gateway = models.CharField(max_length=50, default='africastalking')

    class Meta:
        verbose_name = 'System Settings'
        verbose_name_plural = 'System Settings'

    def __str__(self):
        return "Global System Settings"

    @classmethod
    def get_solo(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj